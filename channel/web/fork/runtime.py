"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from agent.permission import (
    MODES as PERMISSION_MODES,
    global_mode as permission_global_mode,
    normalize_mode as permission_normalize_mode,
)
from auth.runtime import authorized_target, authorized_target_scope
from bridge.context import *
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.chat_message import ChatMessage
# Upstream's core helper, re-exported through ``channel.web.web_channel`` so the
# display-path rewrite has one definition. Used to absolutize workspace-relative
# media refs in replies and history (see ``_send`` / ``HistoryHandler``).
from channel.web.core._common import _rewrite_relative_media
from collections import OrderedDict, deque
from common import const
from common import i18n
from common.log import logger
from common.singleton import singleton
from config import (
    conf,
    get_data_root,
    get_weixin_credentials_path,
    read_config_template,
    sync_image_generation_custom_provider_env,
)
from dataclasses import dataclass, field
from queue import Queue, Empty
from typing import Any, Dict, List, Tuple, Optional, Iterator, NoReturn
from urllib.parse import quote
import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import random
import re
import shutil
import sys
import threading
import time
import uuid
import web

def _live_channel_manager():
    """Return the running ChannelManager, or None before the app is up.

    Resolved through ``common.channel_registry`` (upstream's contract, issue
    #3120): the entry module's private global is not a reliable cell because
    ``python app.py`` makes ``__main__`` a different module object from a later
    ``import app``, so that lookup always yielded None and the console silently
    refused to start a newly configured channel.
    """
    from channel.web.core._common import _live_channel_manager as resolve
    return resolve()


# Adapted on move (not verbatim, see the emitter): ``__file__``
# now points at the fork package, so asset paths anchor at the web
# package root instead of the monolith's own directory.
_WEB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}


VIDEO_EXTENSIONS = {".mp4", ".webm", ".avi", ".mov", ".mkv"}


@dataclass
class SSEStreamState:
    """Bounded, replayable event log for one web request."""

    condition: threading.Condition = field(default_factory=threading.Condition)
    events: deque = field(default_factory=deque)
    next_seq: int = 1
    total_bytes: int = 0
    last_active: float = field(default_factory=time.time)
    main_done: bool = False
    main_done_at: Optional[float] = None
    stream_complete: bool = False
    completed_at: Optional[float] = None
    closed: bool = False


def _parse_sse_cursor(*values) -> int:
    cursors = []
    for value in values:
        try:
            cursors.append(max(0, int(value or 0)))
        except (TypeError, ValueError):
            cursors.append(0)
    return max(cursors, default=0)


def _read_config_file_for_write() -> dict:
    """Baseline dict for a partial write to config.json.

    When the file does not exist yet (fresh install), seed from
    config-template.json — the very config the running process loaded. Starting
    from an empty dict would persist a file missing every template default
    (model, agent limits, ...), silently changing behavior after a restart.
    """
    from channel.web.web_channel import get_data_root
    config_path = os.path.join(get_data_root(), "config.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return read_config_template()


SERVING = threading.Event()


_BIND_ERROR_CODE_RE = re.compile(r"\[(WinError|Errno) (\d+)\]")


def _bind_error_codes(err: OSError):
    """Return ``(winerror, errno)`` for a bind failure.

    cheroot swallows the original exception: it re-raises a bare
    ``socket.error(msg)`` with neither errno nor ``__cause__`` set, so on the
    path we actually care about the code only survives inside the message text.
    """
    winerror = getattr(err, "winerror", None)
    err_no = err.errno
    if winerror is None and err_no is None:
        for kind, code in _BIND_ERROR_CODE_RE.findall(str(err)):
            if kind == "WinError":
                winerror = int(code)
            else:
                err_no = int(code)
    return winerror, err_no


def _log_bind_failure(host: str, port: int, err: OSError):
    """Explain a failed bind in terms the user can act on.

    Windows needs its own branch: a port can be permanently unbindable because
    Hyper-V/WSL2/Docker reserved the range it falls in (WinError 10013), and
    nothing is listening on it, so the usual "kill the stale process" advice
    sends people looking for a process that doesn't exist.
    """
    winerror, err_no = _bind_error_codes(err)
    if winerror == 10013:
        logger.error(
            f"[WebChannel] 端口 {port} 被系统保留，无法绑定（WinError 10013）。"
            f"通常是 Hyper-V/WSL2/Docker 占用了该端口段，可执行 "
            f"`netsh interface ipv4 show excludedportrange protocol=tcp` 查看，"
            f"或在 config.json 中把 web_port 改成区间外的端口"
        )
    elif winerror == 10048 or err_no in (48, 98):  # WSAEADDRINUSE / macOS / Linux
        logger.error(
            f"[WebChannel] 端口 {port} 已被占用，可执行 `cow restart` 清理残留进程，"
            f"或在 config.json 中修改 web_port"
        )
    else:
        logger.error(f"[WebChannel] 无法在 {host}:{port} 上启动服务: {err}")


def _session_expire_seconds():
    from channel.web.web_channel import conf
    return int(conf().get("web_session_expire_days", 30)) * 86400


def _cancel_reply_text(cancelled: int, lang: str) -> str:
    en = lang.startswith("en")
    if cancelled > 0:
        return "🛑 Cancelled" if en else "🛑 已中止"
    return "Nothing to cancel." if en else "当前没有可中止的任务。"


def _steer_reply_text(status, lang: str) -> str:
    from agent.protocol import SteerStatus

    en = (lang or "").lower().startswith("en")
    messages = {
        SteerStatus.ACCEPTED: (
            "↪️ Active task redirected.", "↪️ 已引导当前任务。"
        ),
        SteerStatus.INACTIVE: (
            "No active task to steer.", "当前没有可引导的任务。"
        ),
        SteerStatus.CLOSING: (
            "The active task is already finishing.", "当前任务已结束，无法再引导。"
        ),
        SteerStatus.AMBIGUOUS: (
            "Multiple tasks are active in this session; the steering target is ambiguous.",
            "当前会话有多个任务在运行，无法确定引导目标。",
        ),
        SteerStatus.FULL: (
            "Too many steering updates are pending; try again after the agent processes them.",
            "引导指令过多，请等待当前任务处理后再试。",
        ),
        SteerStatus.INVALID: (
            "Usage: /steer <instruction>", "用法：/steer <引导指令>"
        ),
    }
    english, chinese = messages[status]
    return english if en else chinese


def _get_upload_dir(agent_id: str = None) -> str:
    from agent.registry import get_agent_registry

    workspace = get_agent_registry().get(agent_id).workspace
    upload_dir = os.path.join(workspace, "tmp")
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir


def _get_workspace_root(session_id: str = None, agent_id: str = None) -> str:
    """Resolve the working directory for this request.

    When a session has opened a project directory, that project is the working
    directory the file panel / preview / ``@`` picker operate in. Otherwise it
    is the Agent's workspace (``state_root``, e.g. ``~/cow``). Memory and skills
    always stay in ``state_root`` regardless; only the working root moves.

    In database mode the workspace is derived from the request's ``RuntimeIdentity``:
    a selected tenant resolves its trusted shared root, and the agent must be
    bound to that tenant (no global default fallback). Legacy mode is unchanged.
    """
    from channel.web.web_channel import _is_database_identity
    if session_id:
        try:
            from agent.workspace import project_store
            project_dir = project_store.get_project_dir(session_id, agent_id)
            if project_dir:
                return project_dir
        except Exception as e:
            logger.debug(f"[WebChannel] project_dir resolve failed: {e}")
    # Fork seam (tasks 8.1/8.3): tenancy is resolved by
    # ``channel/web/tenant_workspace.py``, which returns None when the tenant
    # dimension does not apply (legacy mode) and raises when the request must be
    # refused rather than fall back to a global workspace.
    from channel.web.tenant_workspace import resolve_tenant_workspace_root

    scoped_root = resolve_tenant_workspace_root(database_mode=_is_database_identity())
    if scoped_root:
        return scoped_root
    from agent.registry import get_agent_registry

    return get_agent_registry().get(agent_id).workspace


_PREVIEW_SECRET = None


_PREVIEW_SECRET_LOCK = threading.Lock()


def _get_preview_secret() -> bytes:
    """
    Stable secret used to sign /preview directory tokens.

    Preview URLs can't rely on the auth cookie: the preview iframe is sandboxed
    without `allow-same-origin`, so its subresource requests come from an opaque
    origin and Chrome withholds the SameSite=Lax cookie. The signature in the
    URL is what authorizes the request instead, so it must survive restarts.
    """
    from channel.web.web_channel import get_data_root
    global _PREVIEW_SECRET
    if _PREVIEW_SECRET is not None:
        return _PREVIEW_SECRET
    with _PREVIEW_SECRET_LOCK:
        if _PREVIEW_SECRET is not None:
            return _PREVIEW_SECRET
        path = os.path.join(get_data_root(), ".preview_secret")
        secret = None
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    secret = (f.read() or "").strip() or None
        except Exception as e:
            logger.warning(f"[WebChannel] Could not read preview secret: {e}")
        if not secret:
            secret = uuid.uuid4().hex + uuid.uuid4().hex
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(secret)
                os.chmod(path, 0o600)
            except Exception as e:
                logger.warning(f"[WebChannel] Could not persist preview secret: {e}")
        _PREVIEW_SECRET = secret.encode()
        return _PREVIEW_SECRET


def _encode_dir_token(dir_path: str) -> str:
    """Encode a directory path into a signed, URL-safe token for /preview."""
    from channel.web.web_channel import _get_preview_secret
    real = os.path.realpath(dir_path)
    body = base64.urlsafe_b64encode(real.encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(_get_preview_secret(), real.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return f"{body}.{sig}"


def _decode_dir_token(token: str) -> str:
    """Verify and decode a /preview directory token. Raises ValueError if invalid."""
    from channel.web.web_channel import _get_preview_secret
    body, _, sig = (token or "").partition(".")
    if not body or not sig:
        raise ValueError("Malformed preview token")
    padding = "=" * (-len(body) % 4)
    try:
        real = base64.urlsafe_b64decode(body + padding).decode("utf-8")
    except Exception:
        raise ValueError("Malformed preview token")
    expected = hmac.new(_get_preview_secret(), real.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected):
        raise ValueError("Bad preview token signature")
    return real


def _serve_allowed_roots() -> list:
    """Roots that /api/file and /preview may read from (symlinks resolved).

    Includes the configured serve root, the Agent workspace, and any project
    directory a session has opened. Project dirs may live outside the serve
    root (e.g. ``/tmp/foo``), so previewing files in an opened project would
    otherwise be denied.

    Database-only default is the platform file root (never ``~`` or ``/``).
    """
    from channel.web.web_channel import _get_workspace_root
    from channel.web.web_channel import _platform_file_root
    from channel.web.web_channel import conf
    raw = conf().get("web_file_serve_root", None)
    if raw is None or str(raw).strip() == "":
        roots = [os.path.realpath(_platform_file_root())]
    else:
        roots = [os.path.realpath(os.path.expanduser(str(raw)))]
    try:
        roots.append(os.path.realpath(_get_workspace_root()))
    except Exception:
        pass
    try:
        from agent.workspace import project_store
        for rec in project_store.list_recents():
            roots.append(os.path.realpath(rec["path"]))
    except Exception:
        pass
    return roots


def _is_path_allowed(real_path: str) -> bool:
    """True when ``real_path`` is under a database-safe browse root.

    Never trusts operator home / unrestricted serve roots: even if
    ``_serve_allowed_roots`` is widened (tests or misconfig), only the
    platform file root, tenant/Agent workspaces, and opened project dirs
    qualify.
    """
    from channel.web.web_channel import _platform_file_root
    from channel.web.web_channel import _tenant_workspace_roots
    roots = [os.path.realpath(_platform_file_root())]
    # Tenant/Agent roots come from the *static* workspace list rather than
    # ``_get_workspace_root()``: the latter refuses (raises ``web.HTTPError``)
    # when the request carries no tenant identity, and a public capability
    # preview has none. Constructing that error mutates ``web.ctx.status`` and
    # headers even when the exception is swallowed, corrupting the response.
    roots.extend(_tenant_workspace_roots())
    try:
        from agent.workspace import project_store
        for rec in project_store.list_recents():
            roots.append(os.path.realpath(rec["path"]))
    except Exception:
        pass
    for root in roots:
        try:
            if os.path.commonpath([real_path, root]) == root:
                return True
        except ValueError:
            continue
    return False


def _build_preview_url(abs_path: str) -> str:
    """
    Preview URL that mounts the file's *directory*, so relative assets
    referenced by an HTML page (./style.css, ./img/a.png) resolve correctly.
    """
    from channel.web.web_channel import _encode_dir_token
    directory = os.path.dirname(abs_path)
    name = os.path.basename(abs_path)
    return f"/preview/{_encode_dir_token(directory)}/{quote(name)}"


def _build_artifact_payload(data: dict) -> dict:
    """Turn an agent `artifact` event into an SSE payload for the web clients."""
    from channel.web.web_channel import _build_preview_url
    file_path = data.get("path", "")
    if not file_path:
        return None
    return {
        "type": "artifact",
        "abs_path": file_path,
        "rel_path": data.get("rel_path") or os.path.basename(file_path),
        "file_name": data.get("file_name") or os.path.basename(file_path),
        "kind": data.get("kind", "file"),
        "previewable": bool(data.get("previewable")),
        "size": data.get("size", 0),
        "raw_url": f"/api/file?path={quote(file_path)}",
        "preview_url": _build_preview_url(file_path),
    }


def _paths_written_by_step(step: dict) -> list:
    """Files a persisted tool step produced, if any.

    `write`/`edit` name theirs in the arguments. A `subagent` step lists the
    ones its sub agents wrote in its result: those files never passed through
    a tool call of this agent's own, so nothing else records them.
    """
    name = step.get("name")
    if name in ("write", "edit"):
        args = step.get("arguments")
        path = str((args or {}).get("path") or "").strip() if isinstance(args, dict) else ""
        return [path] if path else []
    if name != "subagent":
        return []
    try:
        results = json.loads(step.get("result") or "{}").get("results") or []
    except (ValueError, TypeError, AttributeError):
        return []
    return [
        path
        for item in results if isinstance(item, dict)
        for path in (item.get("files") or [])
    ]


def _artifacts_from_steps(steps, session_id: str = None, agent_id: str = None) -> list:
    """
    Rebuild the artifact cards of a persisted assistant message.

    History replay has no SSE events, so the tool calls are the only record.
    Doing this server-side keeps one implementation of the workspace-internal
    filter — and lets absolute paths inside the workspace be recognised, which
    a client mirroring the rules can't do.

    ``session_id`` anchors detection to the session's working dir (the project
    dir when one is open), matching the live SSE path; otherwise state_root.
    """
    from channel.web.web_channel import _get_workspace_root
    from agent.protocol.artifact import get_workspace_root, safe_build_artifact

    out = []
    seen = set()
    root = None
    for step in steps or []:
        if not isinstance(step, dict) or step.get("type") != "tool" or step.get("is_error"):
            continue
        for path in _paths_written_by_step(step):
            if root is None:
                root = _get_workspace_root(session_id, agent_id) if session_id else get_workspace_root()
            info = safe_build_artifact(path, root)
            if not info or info["path"] in seen:
                continue
            seen.add(info["path"])
            payload = _build_artifact_payload(info)
            if payload:
                out.append(payload)
    return out


def _add_subagent_displays(steps) -> None:
    """Give persisted `subagent` steps the same readable form they had live.

    `display` is deliberately kept out of the model's context, so it is not in
    the stored conversation either. Rebuilding it here means a reloaded page
    shows the sub agents' reports rather than the JSON the model was handed.
    """
    from agent.tools.subagent import format_results

    for step in steps or []:
        if not isinstance(step, dict) or step.get("name") != "subagent":
            continue
        try:
            results = json.loads(step.get("result") or "{}").get("results")
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(results, list) and results:
            step["display"] = format_results(results)


def _add_delegate_displays(steps) -> None:
    """Give persisted `agent_delegate` steps the readable form they had live.

    Same story as `_add_subagent_displays`: `display` is kept out of the model's
    context and so out of storage, so a reloaded page would otherwise show the
    JSON handed to the model rather than "who → whom" and the teammate's reply.
    """
    from agent.tools.agent_delegate.agent_delegate import format_delegate_result

    for step in steps or []:
        if not isinstance(step, dict) or step.get("name") != "agent_delegate":
            continue
        try:
            payload = json.loads(step.get("result") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or not payload.get("content"):
            continue
        source_id = payload.get("delegated_by") or ""
        source_name = source_id
        try:
            from bridge.bridge import Bridge

            source_name = (
                Bridge().get_agent_bridge().agent_registry.get(source_id).name
                or source_id
            )
        except Exception:
            pass
        step["display"] = format_delegate_result(
            source_name,
            payload.get("agent_name") or payload.get("agent_id") or "",
            payload.get("content") or "",
            status=payload.get("status") or "done",
        )


def _sanitize_upload_relative_path(relative_path: str) -> str:
    """Normalize relative upload path and reject escapes / absolute paths."""
    relative_path = (relative_path or "").replace("\\", "/").strip("/")
    if not relative_path:
        raise ValueError("Empty relative path")
    parts = []
    for part in relative_path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("Invalid relative path")
        parts.append(part)
    if not parts:
        raise ValueError("Invalid relative path")
    norm_path = "/".join(parts)
    if os.path.isabs(norm_path):
        raise ValueError("Invalid relative path")
    return norm_path


def _sanitize_upload_id(upload_id: str) -> str:
    """Allow only simple batch ids for directory uploads."""
    sanitized = "".join(ch for ch in (upload_id or "") if ch.isalnum() or ch in ("-", "_"))
    if not sanitized:
        raise ValueError("Invalid upload id")
    return sanitized[:80]


def _is_within_directory(root_path: str, target_path: str) -> bool:
    try:
        return os.path.commonpath([root_path, target_path]) == root_path
    except ValueError:
        return False


def _resolve_upload_path(upload_root: str, relative_path: str) -> Tuple[str, str]:
    """Resolve a relative upload path under upload_root and reject escapes."""
    safe_rel_path = _sanitize_upload_relative_path(relative_path)
    upload_root_real = os.path.realpath(upload_root)
    save_path = os.path.realpath(os.path.join(upload_root_real, *safe_rel_path.split("/")))
    if not _is_within_directory(upload_root_real, save_path):
        raise ValueError("Invalid directory upload path")
    return safe_rel_path, save_path


def _read_uploaded_file_bytes(file_obj) -> bytes:
    """Return uploaded content as bytes across web.py upload object variants."""
    if isinstance(file_obj, bytes):
        return file_obj
    if isinstance(file_obj, str):
        return file_obj.encode("utf-8")

    content = None

    if hasattr(file_obj, "file") and hasattr(file_obj.file, "read"):
        content = file_obj.file.read()
    elif hasattr(file_obj, "read"):
        content = file_obj.read()
    elif hasattr(file_obj, "value"):
        content = file_obj.value

    if content is None:
        raise ValueError("Unable to read uploaded file content")
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    raise TypeError(f"Unsupported uploaded content type: {type(content).__name__}")


def _read_uploaded_file_bytes_limited(file_obj, max_bytes: int) -> bytes:
    """Read uploaded content and fail once it exceeds max_bytes."""
    if isinstance(file_obj, bytes):
        content = file_obj
    elif isinstance(file_obj, str):
        content = file_obj.encode("utf-8")
    elif hasattr(file_obj, "file") and hasattr(file_obj.file, "read"):
        content = file_obj.file.read(max_bytes + 1)
    elif hasattr(file_obj, "read"):
        content = file_obj.read(max_bytes + 1)
    elif hasattr(file_obj, "value"):
        content = file_obj.value
    else:
        raise ValueError("Unable to read uploaded file content")
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not isinstance(content, bytes):
        raise TypeError(f"Unsupported uploaded content type: {type(content).__name__}")
    if len(content) > max_bytes:
        raise ValueError("file too large")
    return content


def _raw_web_input():
    """Return unprocessed multipart form data when web.py exposes rawinput."""
    rawinput = getattr(getattr(web, "webapi", None), "rawinput", None)
    if not callable(rawinput):
        raise RuntimeError("web.py rawinput is not available")
    try:
        return rawinput(method="post")
    except TypeError:
        return rawinput()


def _ensure_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _generate_session_title(user_message: str, assistant_reply: str = "",
                            session_id: str = "") -> str:
    """Delegate to the shared SessionService implementation."""
    from agent.chat.session_service import generate_session_title
    return generate_session_title(user_message, assistant_reply, session_id)


class WebMessage(ChatMessage):
    def __init__(
            self,
            msg_id,
            content,
            ctype=ContextType.TEXT,
            from_user_id="User",
            to_user_id="Chatgpt",
            other_user_id="Chatgpt",
    ):
        self.msg_id = msg_id
        self.ctype = ctype
        self.content = content
        self.from_user_id = from_user_id
        self.to_user_id = to_user_id
        self.other_user_id = other_user_id


@singleton
class WebChannel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = [ReplyType.VOICE]
    _instance = None
    SSE_REPLAY_MAX_EVENTS = 5000
    SSE_REPLAY_MAX_BYTES = 4 * 1024 * 1024
    SSE_POST_DONE_TAIL_SECONDS = 60
    SSE_COMPLETED_TTL_SECONDS = 60
    SSE_IDLE_TIMEOUT_SECONDS = 1800

    # def __new__(cls):
    #     if cls._instance is None:
    #         cls._instance = super(WebChannel, cls).__new__(cls)
    #     return cls._instance

    def __init__(self):
        super().__init__()
        self.msg_id_counter = 0
        self.session_queues = {}  # session_id -> Queue (fallback polling)
        self.request_to_session = {}  # request_id -> session_id
        self.request_to_agent = {}  # request_id -> agent_id
        self.request_owners = {}  # request_id -> immutable (tenant, user, agent, session)
        self.sse_streams = {}  # request_id -> SSEStreamState
        self._sse_streams_lock = threading.RLock()
        self._http_server = None
        self._sse_janitor_started = False

    def _generate_msg_id(self):
        """生成唯一的消息ID"""
        self.msg_id_counter += 1
        return str(int(time.time())) + str(self.msg_id_counter)

    def _generate_request_id(self):
        """生成唯一的请求ID"""
        return str(uuid.uuid4())

    def _publish_sse_event(self, request_id: str, event: dict) -> bool:
        """Append one sequenced event and wake every connected reader."""
        with self._sse_streams_lock:
            state = self.sse_streams.get(request_id)
        if state is None:
            logger.warning(
                f"[WebChannel] dropped SSE event for unknown request "
                f"{request_id}: type={event.get('type')}"
            )
            return False

        with state.condition:
            if state.closed or state.stream_complete:
                reason = "closed" if state.closed else "complete"
                logger.warning(
                    f"[WebChannel] dropped SSE event for {reason} stream "
                    f"{request_id}: type={event.get('type')}"
                )
                return False
            item = dict(event)
            item["seq"] = state.next_seq
            state.next_seq += 1
            encoded_size = len(json.dumps(
                item, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8"))
            state.events.append((item, encoded_size))
            state.total_bytes += encoded_size
            state.last_active = time.time()

            # Keep at least the newest event even if it alone exceeds the byte
            # budget. Cursor expiry is reported explicitly by stream_response.
            while len(state.events) > 1 and (
                len(state.events) > self.SSE_REPLAY_MAX_EVENTS
                or state.total_bytes > self.SSE_REPLAY_MAX_BYTES
            ):
                _, removed_size = state.events.popleft()
                state.total_bytes -= removed_size

            event_type = item.get("type")
            if event_type == "done":
                state.main_done = True
                if state.main_done_at is None:
                    state.main_done_at = state.last_active
            elif event_type == "stream_end":
                state.stream_complete = True
                state.completed_at = state.last_active
            state.condition.notify_all()
        return True

    @staticmethod
    def _session_queue_key(session_id: str, agent_id: str = None) -> str:
        from agent.registry import get_agent_registry
        registry = get_agent_registry()
        resolved = registry.get(agent_id).id
        if resolved == registry.default_agent_id:
            return session_id
        return f"{resolved}::{session_id}"

    def has_session_queue(self, session_id: str, agent_id: str = None) -> bool:
        return self._session_queue_key(session_id, agent_id) in self.session_queues

    def _fetch_latest_pair_seqs(self, session_id: str, agent_id: str = None):
        """Query the conversation store for the latest user/bot message seqs.

        Returned as ``{"user_seq": int|None, "bot_seq": int|None}``; used to
        attach seq metadata onto the SSE ``done`` event so the frontend can
        wire edit / regenerate buttons for live-streamed bubbles without a
        page refresh.
        """
        try:
            from agent.registry import get_agent_registry
            from agent.memory import get_conversation_store
            profile = get_agent_registry().get(agent_id)
            return get_conversation_store(profile.workspace).get_latest_pair_seqs(
                session_id
            )
        except Exception as e:
            logger.debug(f"[WebChannel] _fetch_latest_pair_seqs failed: {e}")
            return {"user_seq": None, "bot_seq": None}

    def send(self, reply: Reply, context: Context):
        try:
            if reply.type in self.NOT_SUPPORT_REPLYTYPE:
                logger.warning(f"Web channel doesn't support {reply.type} yet")
                return

            if reply.type == ReplyType.IMAGE_URL:
                time.sleep(0.5)

            request_id = context.get("request_id", None)
            if not request_id:
                logger.error("No request_id found in context, cannot send message")
                return

            session_id = self.request_to_session.get(request_id)
            if not session_id:
                logger.error(f"No session_id found for request {request_id}")
                return
            agent_id = context.get("agent_id") or self.request_to_agent.get(request_id)
            session_queue_key = self._session_queue_key(session_id, agent_id)

            # SSE mode: append events to the replay log.
            if request_id in self.sse_streams:
                content = reply.content if reply.content is not None else ""

                # Intermediate status lines (e.g. /install-browser phases) must NOT use "done",
                # or the frontend closes EventSource and drops subsequent events.
                if getattr(reply, "sse_phase", False):
                    self._publish_sse_event(request_id, {
                        "type": "phase",
                        "content": content,
                        "request_id": request_id,
                        "timestamp": time.time(),
                    })
                    logger.debug(f"SSE phase for request {request_id}")
                    return

                # Files are already pushed via on_event (file_to_send) during agent execution.
                # Skip duplicate file pushes here; just let the done event through.
                if reply.type in (ReplyType.IMAGE_URL, ReplyType.FILE) and content.startswith("file://"):
                    text_content = getattr(reply, 'text_content', '')
                    with self._sse_streams_lock:
                        state = self.sse_streams.get(request_id)
                    already_done = False
                    if state is not None:
                        with state.condition:
                            already_done = state.main_done
                    # A preceding TEXT reply may already have published done
                    # and deliberately left the stream open for auto-TTS. In
                    # that case this duplicate media reply must not end it.
                    if text_content and not already_done:
                        seqs = self._fetch_latest_pair_seqs(
                            session_id, context.get("agent_id")
                        )
                        published = self._publish_sse_event(request_id, {
                            "type": "done",
                            "content": text_content,
                            "request_id": request_id,
                            "timestamp": time.time(),
                            "user_seq": seqs.get("user_seq"),
                            "bot_seq": seqs.get("bot_seq"),
                        })
                        if published:
                            self._publish_sse_event(
                                request_id, {"type": "stream_end"}
                            )
                    logger.debug(f"SSE skipped duplicate file for request {request_id}")
                    return

                # Skip http-URL FILE/IMAGE_URL replies produced by chat_channel's media extraction:
                # the text reply (already sent as "done") contains the URL and the frontend will
                # render it via renderMarkdown/injectVideoPlayers, so no separate SSE event needed.
                if reply.type in (ReplyType.FILE, ReplyType.IMAGE_URL) and content.startswith(("http://", "https://")):
                    logger.debug(f"SSE skipped http media reply for request {request_id}")
                    return

                seqs = self._fetch_latest_pair_seqs(
                    session_id, context.get("agent_id")
                )
                # Absolutize workspace-relative media so images/videos the agent
                # embedded render for non-default agents too. Only affects the
                # displayed copy; TTS below still reads the original text.
                display_content = content
                if reply.type == ReplyType.TEXT and content:
                    try:
                        display_content = _rewrite_relative_media(
                            content, _get_workspace_root(session_id, context.get("agent_id"))
                        )
                    except Exception as e:
                        logger.debug(f"[WebChannel] media rewrite skipped: {e}")
                self._publish_sse_event(request_id, {
                    "type": "done",
                    "content": display_content,
                    "request_id": request_id,
                    "timestamp": time.time(),
                    "user_seq": seqs.get("user_seq"),
                    "bot_seq": seqs.get("bot_seq"),
                })
                logger.debug(f"SSE done sent for request {request_id}")
                # Auto-trigger TTS once the bot finishes its text reply. The
                # synthesis runs in the background so the chat stream is never
                # blocked; the resulting audio URL is pushed via a follow-up
                # `voice_attach` SSE event and persisted to messages.extras.
                tts_pending = False
                if reply.type == ReplyType.TEXT and content.strip():
                    tts_pending = self._maybe_dispatch_auto_tts(
                        request_id, session_id, content, context
                    )
                if not tts_pending:
                    self._publish_sse_event(request_id, {"type": "stream_end"})
                return

            # Fallback: polling mode
            if session_queue_key in self.session_queues:
                content = reply.content if reply.content is not None else ""
                # Skip file:// IMAGE_URL/FILE replies originating from an SSE-enabled
                # request: they were already pushed via the `file_to_send` event during
                # agent execution. By the time the chat_channel sends the IMAGE_URL reply,
                # the SSE stream has typically closed (after the text "done") and the
                # request_id is gone from sse_streams, so we'd otherwise duplicate the file
                # as a polling bubble. Scheduler/push tasks have no on_event and must
                # still go through polling normally.
                if (
                    reply.type in (ReplyType.IMAGE_URL, ReplyType.FILE)
                    and content.startswith("file://")
                    and context.get("on_event") is not None
                ):
                    logger.debug(f"Polling skipped duplicate file reply for session {session_id}")
                    return
                # SSE-enabled requests already stream the text reply to the
                # client. Do NOT also enqueue it for polling: if the user
                # switched away mid-run, the queued copy would resurface as a
                # duplicate bubble when they return and poll the session.
                if reply.type == ReplyType.TEXT and context.get("on_event") is not None:
                    logger.debug(f"Polling skipped SSE text reply for session {session_id}")
                    return
                # Same workspace-relative media rewrite as the SSE path, so a
                # polled reply from a non-default Agent renders its images too.
                if reply.type == ReplyType.TEXT and content:
                    try:
                        content = _rewrite_relative_media(
                            content,
                            _get_workspace_root(session_id, context.get("agent_id")),
                        )
                    except Exception as e:
                        logger.debug(f"[WebChannel] media rewrite skipped: {e}")
                response_data = {
                    "type": str(reply.type),
                    "content": content,
                    "timestamp": time.time(),
                    "request_id": request_id
                }
                self.session_queues[session_queue_key].put(response_data)
                logger.debug(f"Response sent to poll queue for session {session_id}, request {request_id}")
            else:
                logger.warning(f"No response queue found for session {session_id}, response dropped")

        except Exception as e:
            logger.error(f"Error in send method: {e}")

    def _make_sse_callback(self, request_id: str):
        """Build a callback that publishes agent events to the SSE replay log."""

        # Cap reasoning bytes pushed to the frontend per request to avoid
        # browser stalls / crashes on very long chains-of-thought. Anything
        # beyond the cap is dropped from the stream (DB still persists a
        # truncated copy via _truncate_reasoning_for_storage).
        # Keep aligned with frontend REASONING_RENDER_CAP and backend
        # MAX_STORED_REASONING_CHARS.
        MAX_REASONING_STREAM_CHARS = 4 * 1024  # 4 KB
        # A tool's human-readable outcome (ToolResult.display). Reasoning is a
        # trace worth capping hard; this is the deliverable, so it gets room.
        MAX_DISPLAY_STREAM_CHARS = 32 * 1024
        # Use a single-element list as a mutable counter accessible from closure.
        reasoning_chars_sent = [0]
        reasoning_capped_notified = [False]
        # Captures the first error message emitted by agent_stream so the
        # subsequent agent_end handler can skip its "empty final_response"
        # fallback (which would otherwise overwrite the real error).
        streamed_error: List[str] = []

        def on_event(event: dict):
            if request_id not in self.sse_streams:
                return
            publish = lambda item: self._publish_sse_event(request_id, item)
            event_type = event.get("type")
            data = event.get("data", {})

            if event_type == "reasoning_update":
                delta = data.get("delta", "")
                if not delta:
                    return
                remaining = MAX_REASONING_STREAM_CHARS - reasoning_chars_sent[0]
                if remaining <= 0:
                    if not reasoning_capped_notified[0]:
                        reasoning_capped_notified[0] = True
                        publish({
                            "type": "reasoning",
                            "content": "\n\n... [reasoning truncated for display] ...",
                        })
                    return
                if len(delta) > remaining:
                    delta = delta[:remaining]
                reasoning_chars_sent[0] += len(delta)
                publish({"type": "reasoning", "content": delta})

            elif event_type == "message_update":
                delta = data.get("delta", "")
                if delta:
                    publish({"type": "delta", "content": delta})

            elif event_type == "tool_retrieval":
                # Additive MCP retrieval diagnostics. Forward only the
                # allowlisted, already-sanitized fields (query text/vectors are
                # never included by the emitter and must never reach the client).
                payload = {"type": "tool_retrieval"}
                for key in (
                    "mode", "total_mcp_tools", "selected_mcp_tools",
                    "builtin_tools", "top_k", "candidate_count",
                    "selected_tools", "ranked_tools", "fallback_reason",
                ):
                    if key in data:
                        payload[key] = data[key]
                publish(payload)

            elif event_type == "tool_execution_start":
                tool_name = data.get("tool_name", "tool")
                arguments = data.get("arguments", {})
                publish({"type": "tool_start", "tool_call_id": data.get("tool_call_id"), "tool": tool_name, "arguments": arguments})

            elif event_type == "tool_execution_progress":
                publish({
                    "type": "tool_progress",
                    "tool_call_id": data.get("tool_call_id"),
                    "tool": data.get("tool_name", "tool"),
                    "content": str(data.get("message", ""))[-4 * 1024:],
                })

            elif event_type == "tool_execution_end":
                tool_name = data.get("tool_name", "tool")
                status = data.get("status", "success")
                result = data.get("result", "")
                exec_time = data.get("execution_time", 0)
                # Truncate long results to avoid huge SSE payloads
                result_str = str(result)
                if len(result_str) > 2000:
                    result_str = result_str[:2000] + "…"
                payload = {
                    "type": "tool_end",
                    "tool_call_id": data.get("tool_call_id"),
                    "tool": tool_name,
                    "status": status,
                    "result": result_str,
                    "execution_time": round(exec_time, 2)
                }
                # Carry the permission-refusal marker so the UI can explain why
                # the call was refused. Only a legacy mode refusal carries the
                # mode; database-mode role/isolation refusals carry the kind.
                if data.get("permission_denied"):
                    payload["permission_denied"] = True
                    payload["permission_mode"] = data.get("permission_mode")
                    payload["permission_denial_kind"] = data.get("permission_denial_kind")
                # A tool that wrote its outcome for a person sends that
                # instead. It gets a far larger budget than `result`: this is
                # the report itself, not a trace of how it was produced.
                display = data.get("display")
                if display:
                    display = str(display)
                    if len(display) > MAX_DISPLAY_STREAM_CHARS:
                        display = display[:MAX_DISPLAY_STREAM_CHARS] + "…"
                    payload["display"] = display
                publish(payload)

            elif event_type == "subagent_step":
                # A tool call made by a sub agent, relayed so the card for
                # that sub agent can show what it is doing instead of
                # spinning for minutes.
                publish({
                    "type": "subagent_step",
                    "card_id": data.get("card_id"),
                    "step_id": data.get("step_id"),
                    "phase": data.get("phase"),
                    "tool": data.get("tool_name", "tool"),
                    "arguments": data.get("arguments") or {},
                    "status": data.get("status"),
                    "error": data.get("error"),
                    "execution_time": data.get("execution_time", 0),
                })

            elif event_type == "message_end":
                tool_calls = data.get("tool_calls", [])
                if tool_calls:
                    publish({"type": "message_end", "has_tool_calls": True})

            elif event_type == "error":
                # Agent raised an exception (LLM 401/timeout/etc). Surface the
                # real message instead of letting the empty-response fallback
                # below hide it as "(模型未返回任何内容)".
                err_msg = data.get("error") or "unknown error"
                logger.warning(
                    f"[WebChannel] agent_stream emitted error for "
                    f"request {request_id}: {err_msg}"
                )
                # Remember it so the agent_end handler below knows not to
                # rewrite the message into a generic empty-response notice.
                streamed_error.append(err_msg)
                publish({
                    "type": "done",
                    "content": f"❌ {err_msg}",
                    "request_id": request_id,
                    "timestamp": time.time(),
                })
                publish({"type": "stream_end"})

            elif event_type == "agent_cancelled":
                # Push an explicit cancelled SSE event so the frontend
                # marks the bubble as stopped. A trailing "done" still
                # arrives with the partial answer.
                final_response = data.get("final_response", "")
                publish({
                    "type": "cancelled",
                    "content": final_response,
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

            elif event_type == "agent_end":
                # Safety net: if the agent finishes with an empty final_response,
                # chat_channel skips _send_reply (because reply.content is empty),
                # which means no "done" event is ever emitted and the SSE stream
                # would hang until the 10-min idle timeout. Push a fallback "done"
                # here so the frontend always gets closure.
                final_response = data.get("final_response", "")
                if not final_response or not str(final_response).strip():
                    if streamed_error:
                        # Error was already surfaced via the `error` event
                        # handler above; nothing more to do here.
                        pass
                    else:
                        logger.warning(
                            f"[WebChannel] agent_end with empty final_response for "
                            f"request {request_id}, sending fallback done"
                        )
                        publish({
                            "type": "done",
                            "content": i18n.t(
                                "(模型未返回任何内容，请重试或换一种方式描述你的需求)",
                                "(The model returned no content. Please retry or rephrase your request.)",
                            ),
                            "request_id": request_id,
                            "timestamp": time.time(),
                        })
                        publish({"type": "stream_end"})

            elif event_type == "file_to_send":
                file_path = data.get("path", "")
                file_name = data.get("file_name", os.path.basename(file_path))
                file_type = data.get("file_type", "file")
                # Remote URLs are passed through as-is; local files are served
                # via the backend /api/file endpoint.
                remote_url = data.get("url", "")
                is_remote = bool(remote_url) and remote_url.lower().startswith(("http://", "https://"))
                if is_remote:
                    web_url = remote_url
                else:
                    from urllib.parse import quote
                    web_url = f"/api/file?path={quote(file_path)}"
                is_image = file_type == "image"
                payload = {
                    "type": "image" if is_image else "file",
                    "content": web_url,
                    "file_name": file_name,
                    # Preserve the concrete media kind (image/video/audio/...)
                    # so richer clients can render an inline player.
                    "file_type": file_type,
                }
                # Expose the local absolute path so the desktop client can open
                # the file directly (Finder / default app) instead of the browser.
                if not is_remote and file_path:
                    payload["abs_path"] = file_path
                publish(payload)

            elif event_type == "artifact":
                payload = _build_artifact_payload(data)
                if payload:
                    publish(payload)

        return on_event

    # ------------------------------------------------------------------
    # TTS auto-dispatch
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_voice_reply_mode() -> str:
        """
        Decide the TTS auto-reply policy.

        Source of truth is the cross-channel pair
        (`always_reply_voice`, `voice_reply_voice`) which chat_channel
        also consults. The web UI presents these as a single three-state
        picker (off / voice_if_voice / always) via a lossless mapping.
        """
        from channel.web.web_channel import conf
        if conf().get("always_reply_voice", False):
            return "always"
        if conf().get("voice_reply_voice", False):
            return "voice_if_voice"
        return "off"

    # Mirror of ModelsHandler._TTS_PROVIDERS. zhipu is intentionally omitted
    # from the UI (GLM-TTS prelude beep); pinning it in config.json still works.
    _TTS_PROVIDERS_SUGGEST_ORDER = ["openai", "minimax", "dashscope", "linkai"]

    @classmethod
    def _tts_provider_ready(cls) -> bool:
        """True if user picked a provider OR any suggested vendor has an API key."""
        from channel.web.web_channel import ConfigHandler
        from channel.web.web_channel import conf
        if (conf().get("text_to_voice") or "").strip():
            return True
        for pid in cls._TTS_PROVIDERS_SUGGEST_ORDER:
            meta = ConfigHandler.PROVIDER_MODELS.get(pid) or {}
            key_field = meta.get("api_key_field")
            if not key_field:
                continue
            val = (conf().get(key_field) or "").strip()
            if val and val not in ("YOUR API KEY", "YOUR_API_KEY"):
                return True
        return False

    def _maybe_dispatch_auto_tts(
        self,
        request_id: str,
        session_id: str,
        text: str,
        context: dict,
    ) -> bool:
        try:
            mode = self._resolve_voice_reply_mode()
            if mode == "off":
                return False
            if mode == "voice_if_voice" and not context.get("is_voice_input"):
                return False
            if not self._tts_provider_ready():
                return False
            threading.Thread(
                target=self._synthesize_tts_async,
                args=(request_id, session_id, text, context.get("agent_id")),
                daemon=True,
            ).start()
            return True
        except Exception as e:
            logger.debug(f"[WebChannel] auto-tts dispatch skipped: {e}")
            return False

    def _synthesize_tts_async(
        self,
        request_id: str,
        session_id: str,
        text: str,
        agent_id: str = None,
    ) -> None:
        try:
            from bridge.bridge import Bridge
            reply = Bridge().fetch_text_to_voice(text)
            if reply is None or reply.type != ReplyType.VOICE or not reply.content:
                logger.warning(
                    f"[WebChannel] TTS produced no audio for request {request_id}: "
                    f"reply={reply}"
                )
                return
            url = self._publish_tts_audio(reply.content, agent_id)
            if not url:
                logger.warning(f"[WebChannel] TTS publish failed for request {request_id}")
                return
            payload = {"audio": {"url": url, "kind": "tts"}}
            try:
                from agent.memory import get_conversation_store
                from agent.registry import get_agent_registry
                profile = get_agent_registry().get(agent_id)
                get_conversation_store(
                    profile.workspace
                ).attach_extras_to_last_assistant(session_id, payload)
            except Exception as e:
                logger.debug(f"[WebChannel] tts persist skipped: {e}")
            if request_id not in self.sse_streams:
                logger.warning(
                    f"[WebChannel] TTS ready but SSE stream already closed "
                    f"for request {request_id} (url={url})"
                )
                return
            self._publish_sse_event(request_id, {
                "type": "voice_attach",
                "url": url,
                "request_id": request_id,
                "timestamp": time.time(),
            })
            logger.info(f"[WebChannel] TTS voice_attach pushed for request {request_id}: {url}")
        except Exception as e:
            # TTS failures are intentionally silent (no user-facing error).
            logger.warning(f"[WebChannel] TTS synthesis failed: {e}")
        finally:
            self._publish_sse_event(request_id, {"type": "stream_end"})

    @staticmethod
    def _publish_tts_audio(src_path: str, agent_id: str = None) -> str:
        """Move a TTS file into uploads/ and return its public URL."""
        from channel.web.web_channel import _get_upload_dir
        try:
            if not src_path or not os.path.isfile(src_path):
                logger.warning(f"[WebChannel] publish_tts_audio missing source: {src_path!r}")
                return ""
            ext = os.path.splitext(src_path)[1].lower() or ".mp3"
            upload_dir = _get_upload_dir(agent_id)
            os.makedirs(upload_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            dst_name = f"voice_reply_{ts}_{random.randint(0, 9999)}{ext}"
            dst_path = os.path.join(upload_dir, dst_name)
            shutil.move(src_path, dst_path)
            logger.debug(f"[WebChannel] publish_tts_audio moved {src_path} -> {dst_path}")
            suffix = f"?agent_id={agent_id}" if agent_id else ""
            return f"/uploads/{dst_name}{suffix}"
        except Exception as e:
            logger.warning(f"[WebChannel] publish_tts_audio failed: {e}")
            return ""

    @staticmethod
    def _cleanup_stale_voice_recordings(max_age_seconds: int = 3600) -> None:
        """Drop voice_input_* uploads older than max_age_seconds (run at startup)."""
        from channel.web.web_channel import _get_upload_dir
        try:
            upload_dir = _get_upload_dir()
            if not os.path.isdir(upload_dir):
                return
            now = time.time()
            removed = 0
            for name in os.listdir(upload_dir):
                if not name.startswith("voice_input_"):
                    continue
                full = os.path.join(upload_dir, name)
                try:
                    if not os.path.isfile(full):
                        continue
                    if now - os.path.getmtime(full) > max_age_seconds:
                        os.remove(full)
                        removed += 1
                except OSError:
                    continue
            if removed:
                logger.info(f"[WebChannel] cleaned up {removed} stale voice recording(s) from {upload_dir}")
        except Exception as e:
            logger.warning(f"[WebChannel] voice cleanup failed: {e}")

    def upload_file(self):
        """Handle file or directory upload via multipart/form-data.

        The target Agent is not a parameter: the handler authorizes it (tenant
        binding, private-owner rule, ``agent.use``) and publishes it through the
        request-scoped authorized target (tasks 8.2/8.3), so a cross-tenant
        ``agent_id`` in the form field can never steer the write and upstream's
        signature stays exactly as upstream wrote it.
        """
        from channel.web.web_channel import _get_upload_dir
        from channel.web.web_channel import _raw_web_input
        agent_id = authorized_target().get("agent_id")

        def _reject(message):
            logger.warning("[WebChannel] Upload rejected: %s", message)
            return json.dumps({"status": "error", "message": message})

        try:
            # Trace the request on arrival: it is the only way to tell a client
            # that never sent anything (file picker / drag-drop broken) apart
            # from a request the backend rejected.
            logger.info(
                "[WebChannel] Upload request received: %s bytes, content-type=%s",
                web.ctx.env.get("CONTENT_LENGTH") or "?",
                web.ctx.env.get("CONTENT_TYPE") or "?",
            )
            params = _raw_web_input()
            file_obj = params.get("file")
            file_objs = params.get("files")
            session_id = params.get("session_id", "")
            relative_path = params.get("relative_path", "")
            relative_paths = params.get("relative_paths")
            upload_id = params.get("upload_id", "")

            directory_files = _ensure_list(file_objs)

            # NOTE: cgi.FieldStorage raises TypeError on truthy checks for single-file
            # uploads (Python 3.9+). Always use `is not None` instead of `if file_obj`.
            if not directory_files and file_obj is not None and relative_path:
                directory_files = [file_obj]

            directory_rel_paths = _ensure_list(relative_paths)

            if not directory_rel_paths and relative_path:
                directory_rel_paths = [relative_path]

            is_directory_upload = bool(directory_files) or bool(directory_rel_paths) or bool(relative_path) or bool(upload_id)

            # ``agent_id`` comes from the authorized target; the fallback keeps
            # the pre-authorization behaviour for embedders that publish none,
            # and has to read the query string too — a multipart body carries no
            # ``agent_id`` (see ``_scoped_agent_id``), so the body-only helper
            # would write the upload into the default Agent's workspace.
            upload_dir = _get_upload_dir(agent_id or _scoped_agent_id(params))
            if is_directory_upload:
                if not upload_id:
                    return _reject("Missing upload_id for directory upload")
                if not directory_files:
                    return _reject("No files uploaded")
                if len(directory_files) != len(directory_rel_paths):
                    return _reject("Directory upload payload mismatch")

                safe_upload_id = _sanitize_upload_id(upload_id)
                upload_root = os.path.join(upload_dir, f"webdir_{safe_upload_id}")
                upload_root_real = os.path.realpath(upload_root)

                root_name = None
                saved_files = 0
                for file_obj, rel_path in zip(directory_files, directory_rel_paths):
                    if file_obj is None:
                        raise ValueError("Invalid uploaded file")
                    safe_rel_path, save_path = _resolve_upload_path(upload_root_real, rel_path)
                    current_root_name = safe_rel_path.split("/", 1)[0]
                    if root_name is None:
                        root_name = current_root_name
                    elif root_name != current_root_name:
                        raise ValueError("Directory upload must use a single root folder")
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    content_bytes = _read_uploaded_file_bytes(file_obj)
                    with open(save_path, "wb") as f:
                        f.write(content_bytes)
                    saved_files += 1

                if not root_name:
                    raise ValueError("Directory root path missing")

                root_path = os.path.realpath(os.path.join(upload_root_real, root_name))
                if not _is_within_directory(upload_root_real, root_path):
                    raise ValueError("Invalid directory upload path")

                logger.info(f"[WebChannel] Directory uploaded: {root_name} -> {root_path} ({saved_files} files)")
                return json.dumps({
                    "status": "success",
                    "file_path": root_path,
                    "file_name": root_name,
                    "file_type": "directory",
                    "file_count": saved_files,
                    "root_path": root_path,
                    "root_name": root_name,
                    "upload_type": "directory",
                }, ensure_ascii=False)

            if file_obj is None or not hasattr(file_obj, "filename") or not file_obj.filename:
                return _reject(f"No file uploaded (form fields: {sorted(params.keys())})")

            original_name = file_obj.filename
            ext = os.path.splitext(original_name)[1].lower()
            safe_name = f"web_{uuid.uuid4().hex[:8]}{ext}"
            save_path = os.path.join(upload_dir, safe_name)
            public_path = safe_name
            display_name = original_name

            content_bytes = _read_uploaded_file_bytes(file_obj)
            with open(save_path, "wb") as f:
                f.write(content_bytes)

            if ext in IMAGE_EXTENSIONS:
                file_type = "image"
            elif ext in VIDEO_EXTENSIONS:
                file_type = "video"
            else:
                file_type = "file"

            from urllib.parse import quote
            preview_url = f"/uploads/{quote(public_path, safe='/')}"
            # Name the writing Agent explicitly. The read-back route derives its
            # tenant from the addressed Agent's binding, and the browser loads
            # this URL directly as an <img>/<audio> subresource (so the console's
            # fetch wrapper never sees it). Without the id the read would fall
            # back to the tenant's *default* Agent and 404 for any other one.
            if agent_id:
                preview_url += f"?agent_id={quote(str(agent_id), safe='')}"

            logger.info(f"[WebChannel] File uploaded: {original_name} -> {save_path} ({file_type})")

            return json.dumps({
                "status": "success",
                "file_path": save_path,
                "file_name": display_name,
                "file_type": file_type,
                "preview_url": preview_url,
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"[WebChannel] File upload error: {e}", exc_info=True)
            return json.dumps({"status": "error", "message": str(e)})

    def post_message(self):
        """
        Handle incoming messages from users via POST request.
        Returns a request_id for tracking this specific request.
        Supports optional attachments (file paths from /upload).

        Any database-mode chat context (the resolved tenant membership and the
        already-authorized ``(agent_id, session_id)`` pair) arrives through the
        request-scoped authorized target rather than as parameters, so upstream's
        signature and body stay mergeable (tasks 8.2/8.3). With no target
        published this behaves exactly as upstream: the Agent is resolved by the
        router from the request body.
        """
        from channel.web.web_channel import Queue
        from channel.web.web_channel import SSEStreamState
        from channel.web.web_channel import _addressed_agent_id
        from channel.web.web_channel import _get_workspace_root
        from channel.web.web_channel import _require_agent_action
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_tenant_agent_binding
        from channel.web.web_channel import _session_roster
        from channel.web.web_channel import _web_runtime_identity_snapshot
        from channel.web.web_channel import conf
        target = authorized_target()
        auth_context = target.get("auth_context")
        authorized_session = target.get("session")
        try:
            data = web.data()
            json_data = json.loads(data)
            session_id = json_data.get('session_id', f'session_{int(time.time())}')
            from bridge.bridge import Bridge
            agent_bridge = Bridge().get_agent_bridge()
            if authorized_session is not None:
                resolved_agent_id, session_id = authorized_session
            else:
                resolved_agent_id = agent_bridge.agent_router.resolve(
                    explicit_agent_id=json_data.get("agent_id"),
                )
            prompt = json_data.get('message', '')
            # Kept before any prefixing or attachment lines, so mention parsing
            # still sees what the user actually typed.
            typed_prompt = prompt
            use_sse = json_data.get('stream', True)
            attachments = json_data.get('attachments', [])
            # Tag the message as originating from voice input so the post-reply
            # TTS hook can honour the `voice_if_voice` policy (mirrors the
            # desire_rtype concept used by other channels).
            is_voice_input = bool(json_data.get('is_voice', False))

            # Fast path for /cancel: bypass the session queue and SSE setup.
            # Web frontend (stream=true) only listens to SSE, so we return an
            # inline_reply payload to be rendered synchronously.
            stripped_prompt = (prompt or "").strip().lower()
            if stripped_prompt == "/cancel":
                from agent.protocol import get_cancel_registry
                scoped_session_id = agent_bridge._cancel_key(
                    resolved_agent_id,
                    session_id,
                    agent_bridge.agent_registry.default_agent_id,
                )
                cancelled = get_cancel_registry().cancel_session(scoped_session_id)
                lang = (json_data.get('lang') or 'zh').lower()
                msg_text = _cancel_reply_text(cancelled, lang)
                logger.info(
                    f"[WebChannel] /cancel fast-path: session={session_id}, cancelled={cancelled}, lang={lang}"
                )
                return json.dumps({
                    "status": "success",
                    "request_id": "",
                    "stream": False,
                    "inline_reply": msg_text,
                })

            # Explicit steering also bypasses the normal session queue. The
            # Web button sends ``steer: true`` with raw input; typed /steer
            # commands use the same endpoint and semantics as IM channels.
            steer_requested = bool(json_data.get("steer", False))
            is_steer_command = (
                re.match(r"^/steer(?:\s|$)", stripped_prompt) is not None
            )
            if steer_requested or is_steer_command:
                instruction = (
                    (prompt or "").strip()[len("/steer"):].strip()
                    if is_steer_command
                    else (prompt or "").strip()
                )
                result = agent_bridge.steer_session(
                    session_id, instruction, resolved_agent_id
                )
                lang = (json_data.get("lang") or "zh").lower()
                msg_text = _steer_reply_text(result.status, lang)
                logger.info(
                    f"[WebChannel] steer fast-path: session={session_id}, "
                    f"status={result.status.value}, lang={lang}"
                )
                return json.dumps({
                    "status": "success",
                    "request_id": "",
                    "stream": False,
                    "steered": result.accepted,
                    "inline_reply": msg_text,
                }, ensure_ascii=False)

            # Append file references to the prompt (same format as QQ channel)
            context_attachments = []
            if attachments:
                file_refs = []
                for att in attachments:
                    ftype = att.get("file_type", "file")
                    fpath = att.get("file_path", "")
                    if not fpath:
                        continue
                    if ftype == "workspace_ref":
                        # Already lives in the workspace (dragged from the file panel
                        # or picked with @); reference it in place so the agent opens
                        # the original instead of an uploaded copy. Naming the kind
                        # tells the agent whether to `read` it or `ls` into it.
                        # Resolve relative to the session's working root (project
                        # dir when opened, else the workspace).
                        is_dir = os.path.isdir(
                            os.path.join(
                                _get_workspace_root(session_id, resolved_agent_id), fpath
                            )
                        )
                        label = (
                            i18n.t('工作空间目录', 'Workspace directory') if is_dir
                            else i18n.t('工作空间文件', 'Workspace file')
                        )
                        file_refs.append(f"[{label}: {fpath}]")
                    elif ftype == "image":
                        file_refs.append(f"[{i18n.t('图片', 'Image')}: {fpath}]")
                        # The path marker above stays (it is what history shows
                        # and what a text-only model can still reason about),
                        # but the image itself is delivered as a content part
                        # when this turn's model accepts one.
                        context_attachments.append({
                            "path": fpath,
                            "file_type": "image",
                        })
                    elif ftype == "video":
                        file_refs.append(f"[{i18n.t('视频', 'Video')}: {fpath}]")
                    elif ftype == "directory":
                        file_refs.append(f"[{i18n.t('目录', 'Directory')}: {fpath}]")
                    else:
                        file_refs.append(f"[{i18n.t('文件', 'File')}: {fpath}]")
                if file_refs:
                    prompt = prompt + "\n" + "\n".join(file_refs)
                    logger.info(f"[WebChannel] Attached {len(file_refs)} file(s) to message")

            request_id = self._generate_request_id()
            with self._sse_streams_lock:
                self.request_to_session[request_id] = session_id
                self.request_to_agent[request_id] = resolved_agent_id
                if auth_context is not None:
                    self.request_owners[request_id] = (
                        auth_context.tenant_id, auth_context.user_id, resolved_agent_id, session_id,
                    )

            session_queue_key = self._session_queue_key(
                session_id, resolved_agent_id
            )
            if session_queue_key not in self.session_queues:
                self.session_queues[session_queue_key] = Queue()

            if use_sse:
                with self._sse_streams_lock:
                    self.sse_streams[request_id] = SSEStreamState()

            trigger_prefixs = conf().get("single_chat_prefix", [""])
            if check_prefix(prompt, trigger_prefixs) is None:
                if trigger_prefixs:
                    prompt = trigger_prefixs[0] + prompt
                    logger.debug(f"[WebChannel] Added prefix to message: {prompt}")

            msg = WebMessage(self._generate_msg_id(), prompt)
            msg.from_user_id = session_id

            context = self._compose_context(ContextType.TEXT, prompt, msg=msg, isgroup=False)

            if context is None:
                logger.warning(f"[WebChannel] Context is None for session {session_id}, message may be filtered")
                self._drop_sse_request(request_id)
                return json.dumps({"status": "error", "message": "Message was filtered"})

            context["session_id"] = session_id
            context["receiver"] = session_id
            context["request_id"] = request_id
            context["agent_id"] = resolved_agent_id
            if context_attachments:
                # Structured inbound images for the agent turn. The prompt keeps
                # its path markers; this is the machine-readable counterpart.
                context["attachments"] = context_attachments
            # Addressing a teammate hands them the turn. The conversation still
            # belongs to `resolved_agent_id`, so this only changes who answers.
            # The composer already knows who it wrote; parsing the text is the
            # fallback for a mention typed by hand or replayed from history.
            roster = _session_roster(session_id, resolved_agent_id)
            addressed = (json_data.get("speaker_agent_id") or "").strip()
            if not addressed or not any(item["id"] == addressed for item in roster):
                addressed = _addressed_agent_id(typed_prompt, roster)
            if addressed and addressed != resolved_agent_id:
                if auth_context is not None:
                    _require_tenant_agent_binding(auth_context, addressed)
                    _require_private_owner(auth_context, addressed)
                    # Handing the turn to a teammate still runs that Agent, so it
                    # needs the same execution grant as a direct dispatch.
                    _require_agent_action(auth_context, addressed, "use", "agent.use")
                context["speaker_agent_id"] = addressed
            if is_voice_input:
                # Web channel runs its own TTS post-pipeline via
                # _maybe_dispatch_auto_tts; don't set desire_rtype here or
                # chat_channel would synthesize a duplicate VOICE reply.
                context["is_voice_input"] = True

            if use_sse:
                context["on_event"] = self._make_sse_callback(request_id)

            # In database identity mode the ambient identity (tenant/user) is
            # scoped by the enclosing _db_scope. The run is dispatched on a
            # separate thread where ContextVars don't carry, so snapshot the
            # identity onto the context here and let _identity_for rebuild it.
            context["runtime_identity"] = _web_runtime_identity_snapshot()

            threading.Thread(target=self.produce, args=(context,)).start()

            return json.dumps({
                "status": "success",
                "request_id": request_id,
                "stream": use_sse,
                # Lets the live bubble carry the right name and face while the
                # reply streams, before any of it has been persisted.
                "speaker": context.get("speaker_agent_id") or "",
            })

        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _drop_sse_request(self, request_id: str):
        """Reclaim all state tied to an SSE request."""
        with self._sse_streams_lock:
            state = self.sse_streams.pop(request_id, None)
            self.request_to_session.pop(request_id, None)
            self.request_to_agent.pop(request_id, None)
            getattr(self, "request_owners", {}).pop(request_id, None)
        if state is not None:
            with state.condition:
                state.closed = True
                state.condition.notify_all()

    def _sweep_sse_streams(self, now: Optional[float] = None) -> int:
        """Finalize overdue tails and reclaim expired SSE replay logs."""
        now = time.time() if now is None else now
        with self._sse_streams_lock:
            states = list(self.sse_streams.items())

        overdue = []
        for request_id, state in states:
            with state.condition:
                if (
                    state.main_done
                    and not state.stream_complete
                    and state.main_done_at is not None
                    and now - state.main_done_at
                    >= self.SSE_POST_DONE_TAIL_SECONDS
                ):
                    overdue.append(request_id)
        for request_id in overdue:
            self._publish_sse_event(request_id, {"type": "stream_end"})

        with self._sse_streams_lock:
            states = list(self.sse_streams.items())
        stale = []
        for request_id, state in states:
            with state.condition:
                if state.stream_complete and state.completed_at is not None:
                    expired = (
                        now - state.completed_at
                        >= self.SSE_COMPLETED_TTL_SECONDS
                    )
                else:
                    expired = (
                        now - state.last_active
                        >= self.SSE_IDLE_TIMEOUT_SECONDS
                    )
            if expired:
                stale.append(request_id)

        for request_id in stale:
            self._drop_sse_request(request_id)
        return len(stale)

    def _start_sse_janitor(self):
        """Start a background thread that reclaims orphaned SSE logs.

        Completed logs remain replayable for a short grace period. Abandoned
        unfinished logs use the longer idle timeout.
        """
        if self._sse_janitor_started:
            return
        self._sse_janitor_started = True

        SWEEP_INTERVAL = 60

        def _sweep():
            while True:
                time.sleep(SWEEP_INTERVAL)
                try:
                    reclaimed = self._sweep_sse_streams()
                    if reclaimed:
                        logger.info(
                            f"[WebChannel] SSE janitor reclaimed {reclaimed} "
                            f"idle stream(s)"
                        )
                except Exception as e:
                    logger.warning(f"[WebChannel] SSE janitor error: {e}")

        t = threading.Thread(target=_sweep, name="sse-janitor", daemon=True)
        t.start()

    def stream_response(self, request_id: str, after_seq: int = 0):
        """
        SSE generator for a given request_id.
        Yields UTF-8 encoded bytes to avoid WSGI Latin-1 mangling.
        Each connection reads the request's event log using its own cursor.
        """
        with self._sse_streams_lock:
            state = self.sse_streams.get(request_id)
        if state is None:
            yield b"data: {\"type\": \"error\", \"message\": \"invalid request_id\"}\n\n"
            return
        try:
            cursor = max(0, int(after_seq))
        except (TypeError, ValueError):
            cursor = 0
        idle_timeout = 600  # 10 minutes without any real event
        deadline = time.time() + idle_timeout
        # A cancel only takes effect at the agent's next checkpoint, so the run
        # keeps emitting events (tool results, the partial reply) for a while
        # after the user presses Stop. Stay open for them, just not for the
        # full idle timeout.
        CANCEL_GRACE_SECONDS = 60
        cancelled = False

        try:
            while time.time() < deadline:
                resync_payload = None
                force_stream_end = False
                with state.condition:
                    now = time.time()
                    state.last_active = now
                    force_stream_end = (
                        state.main_done
                        and not state.stream_complete
                        and state.main_done_at is not None
                        and now - state.main_done_at
                        >= self.SSE_POST_DONE_TAIL_SECONDS
                    )
                    if state.events:
                        first_seq = state.events[0][0]["seq"]
                        latest_seq = state.events[-1][0]["seq"]
                        if cursor < first_seq - 1:
                            resync_payload = {
                                "type": "resync_required",
                                "reason": "event_cursor_expired",
                                "after_seq": cursor,
                                "first_available_seq": first_seq,
                            }
                        elif cursor > latest_seq:
                            resync_payload = {
                                "type": "resync_required",
                                "reason": "event_cursor_ahead",
                                "after_seq": cursor,
                                "latest_available_seq": latest_seq,
                            }
                    pending = [
                        event for event, _ in state.events
                        if event["seq"] > cursor
                    ]
                    complete = state.stream_complete
                    closed = state.closed
                    if (
                        resync_payload is None
                        and not pending and not complete and not closed
                    ):
                        state.condition.wait(timeout=1)

                if force_stream_end:
                    self._publish_sse_event(
                        request_id, {"type": "stream_end"}
                    )
                    continue

                if resync_payload is not None:
                    payload = json.dumps(resync_payload, ensure_ascii=False)
                    yield f"data: {payload}\n\n".encode("utf-8")
                    return

                if not pending:
                    if complete or closed:
                        break
                    yield b": keepalive\n\n"
                    continue

                for item in pending:
                    deadline = time.time() + (
                        CANCEL_GRACE_SECONDS if cancelled else idle_timeout
                    )
                    payload = json.dumps(item, ensure_ascii=False)
                    yield (
                        f"id: {item['seq']}\n"
                        f"data: {payload}\n\n"
                    ).encode("utf-8")
                    cursor = item["seq"]
                    if item.get("type") == "cancelled":
                        cancelled = True
                        deadline = time.time() + CANCEL_GRACE_SECONDS
                    if item.get("type") == "stream_end":
                        return
        except GeneratorExit:
            # The event log is deliberately retained for reconnection.
            raise

    def cancel_request(self):
        """
        Cancel an in-flight agent run.

        Body: {"request_id": "...", "session_id": "..."}
        Either field is sufficient; request_id is preferred when known.
        Always returns success even when nothing was running, so the
        client's UX is idempotent.

        The authorized ``(agent_id, session_id)`` pair, when the handler
        resolved one, arrives through the request-scoped authorized target
        (tasks 8.2/8.3) instead of a rewritten signature.
        """
        authorized_session = authorized_target().get("session")
        try:
            from agent.protocol import get_cancel_registry

            data = web.data()
            try:
                json_data = json.loads(data) if data else {}
            except Exception:
                json_data = {}

            request_id = (json_data.get("request_id") or "").strip()
            session_id = (json_data.get("session_id") or "").strip()
            lang = (json_data.get("lang") or "zh").lower()
            from bridge.bridge import Bridge
            from agent.routing import AgentUnavailableError
            agent_bridge = Bridge().get_agent_bridge()
            agent_id = self.request_to_agent.get(request_id)
            if authorized_session is not None:
                agent_id, session_id = authorized_session
            if not agent_id:
                try:
                    agent_id = agent_bridge.agent_router.resolve(
                        explicit_agent_id=json_data.get("agent_id"),
                    )
                except AgentUnavailableError:
                    # Session pinned to a since-deleted Agent; nothing in flight
                    # for it to cancel. Report success with a zero count rather
                    # than raising on every cancel attempt.
                    return json.dumps({"status": "success", "cancelled": 0})

            registry = get_cancel_registry()
            cancelled = 0

            if request_id:
                if registry.cancel_request(request_id):
                    cancelled = 1

            if cancelled == 0 and session_id:
                scoped_session_id = agent_bridge._cancel_key(
                    agent_id,
                    session_id,
                    agent_bridge.agent_registry.default_agent_id,
                )
                cancelled = registry.cancel_session(scoped_session_id)

            if request_id and request_id in self.sse_streams:
                self._publish_sse_event(request_id, {
                    "type": "cancelled",
                    "content": "🛑 Cancelled" if lang.startswith("en") else "🛑 已中止",
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

            logger.info(
                f"[WebChannel] cancel request: request_id={request_id!r}, "
                f"session_id={session_id!r}, cancelled={cancelled}"
            )
            return json.dumps({
                "status": "success",
                "cancelled": cancelled,
            })

        except Exception as e:
            logger.error(f"[WebChannel] cancel_request error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def poll_response(self):
        """
        Poll for responses using the session_id.

        The authorized ``(agent_id, session_id)`` pair, when the handler
        resolved one, arrives through the request-scoped authorized target
        (tasks 8.2/8.3) instead of a rewritten signature.
        """
        authorized_session = authorized_target().get("session")
        try:
            data = web.data()
            json_data = json.loads(data)
            session_id = json_data.get('session_id')
            from bridge.bridge import Bridge
            from agent.routing import AgentUnavailableError
            agent_bridge = Bridge().get_agent_bridge()
            try:
                if authorized_session is not None:
                    agent_id, session_id = authorized_session
                else:
                    agent_id = agent_bridge.agent_router.resolve(
                        explicit_agent_id=json_data.get("agent_id"),
                    )
            except AgentUnavailableError:
                # The session is pinned to an Agent that has since been deleted
                # or disabled (a stale client selection). Polling is read-only,
                # so there is nothing to answer - report no content instead of
                # raising every tick, which otherwise floods the log.
                return json.dumps({
                    "status": "success",
                    "has_content": False,
                    "agent_unavailable": True,
                })
            session_queue_key = self._session_queue_key(session_id, agent_id)

            if not session_id or session_queue_key not in self.session_queues:
                return json.dumps({"status": "error", "message": "Invalid session ID"})

            # 尝试从队列获取响应，不等待
            try:
                # 使用peek而不是get，这样如果前端没有成功处理，下次还能获取到
                response = self.session_queues[session_queue_key].get(block=False)

                # 返回响应，包含请求ID以区分不同请求
                return json.dumps({
                    "status": "success",
                    "has_content": True,
                    "content": response["content"],
                    "request_id": response["request_id"],
                    "timestamp": response["timestamp"]
                })

            except Empty:
                # 没有新响应
                return json.dumps({"status": "success", "has_content": False})

        except Exception as e:
            logger.error(f"Error polling response: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def chat_page(self):
        """Serve the chat HTML page."""
        from channel.web.web_channel import _web_navigation_mode
        from channel.web.web_channel import _workbench_sidebar_launch_v2
        file_path = os.path.join(_WEB_ROOT, 'chat.html')  # 使用绝对路径
        with open(file_path, 'r', encoding='utf-8') as f:
            html = f.read()
        # Inject the backend-resolved default language so the console can use
        # it on first load (when the user has no saved cow_lang preference).
        html = html.replace("{{COW_DEFAULT_LANG}}", i18n.get_language())
        html = html.replace("{{COW_NAVIGATION_MODE}}", _web_navigation_mode())
        # Temporary workbench-sidebar presentation switch (layout only).
        return html.replace(
            "{{COW_WORKBENCH_SIDEBAR_LAUNCH_V2}}",
            _workbench_sidebar_launch_v2(),
        )

    def startup(self):
        from channel.web.web_channel import SERVING
        from channel.web.web_channel import _WEB_URLS
        from channel.web.web_channel import build_web_app
        from channel.web.web_channel import conf
        configured_host = conf().get("web_host", "")
        # Database identity is always required; default to loopback unless
        # the operator explicitly publishes via web_host.
        host = configured_host or "127.0.0.1"
        # The desktop app passes its chosen port via COW_WEB_PORT so its backend
        # never collides with a source-run web console (default 9899). This makes
        # the port a single source of truth owned by the Electron shell.
        port = int(os.environ.get("COW_WEB_PORT") or conf().get("web_port", 9899))
        is_public_bind = host in ("0.0.0.0", "::")

        self._cleanup_stale_voice_recordings()

        def _log_startup_banner():
            """Announce the console. Only called once the socket is actually
            bound — printing it up front made a failed bind look like a
            successful startup in the logs."""
            # Print available channel types (ordered by language: prioritize
            # locally-popular channels for the current UI language)
            logger.info(
                "[WebChannel] Available channels (edit `channel_type` in config.json to switch, separate multiple with commas):")
            zh_channels = [
                ("web", "Web"),
                ("terminal", "Terminal"),
                ("weixin", "WeChat"),
                ("feishu", "Feishu"),
                ("dingtalk", "DingTalk"),
                ("wecom_bot", "WeCom Bot"),
                ("wechatcom_app", "WeCom App"),
                ("wechat_kf", "WeChat Customer Service"),
                ("wechatmp", "WeChat Official Account"),
                ("wechatmp_service", "WeChat Official Account (Service)"),
                ("telegram", "Telegram"),
                ("slack", "Slack"),
                ("discord", "Discord"),
            ]
            en_channels = [
                ("web", "Web"),
                ("terminal", "Terminal"),
                ("telegram", "Telegram"),
                ("slack", "Slack"),
                ("discord", "Discord"),
                ("weixin", "WeChat"),
                ("feishu", "Feishu"),
                ("dingtalk", "DingTalk"),
                ("wecom_bot", "WeCom Bot"),
                ("wechatcom_app", "WeCom App"),
                ("wechat_kf", "WeChat Customer Service"),
                ("wechatmp", "WeChat Official Account"),
                ("wechatmp_service", "WeChat Official Account (Service)"),
            ]
            channels = en_channels if i18n.get_language() == "en" else zh_channels
            name_width = max(len(name) for name, _ in channels)
            for idx, (name, label) in enumerate(channels, 1):
                logger.info(f"[WebChannel]  {idx:>2}. {name:<{name_width}} - {label}")
            logger.info("[WebChannel] ✅ Web console is running")
            logger.info(f"[WebChannel] 🌐 Local access: http://localhost:{port}")
            if is_public_bind:
                logger.info(f"[WebChannel] 🌍 Server access: http://YOUR_IP:{port} (replace YOUR_IP with your server IP)")
                logger.info("[WebChannel] 🔒 Database identity is required for all console access")
            else:
                logger.info(f"[WebChannel] 🔒 Listening on {host} only (local access). For public access, set web_host to 0.0.0.0 (database login still required)")

            # In desktop mode the Electron shell renders the UI, so don't pop a
            # browser window (also avoids issues when running detached/headless).
            if os.environ.get("COW_DESKTOP") != "1":
                try:
                    import webbrowser
                    webbrowser.open(f"http://localhost:{port}")
                    logger.debug(f"[WebChannel] Opened browser at http://localhost:{port}")
                except Exception as e:
                    logger.debug(f"[WebChannel] Could not open browser: {e}")

        # Ensure the static dir exists. In a packaged build it ships read-only
        # inside the bundle, so swallow errors instead of failing startup.
        static_dir = os.path.join(_WEB_ROOT, 'static')
        if not os.path.exists(static_dir):
            try:
                os.makedirs(static_dir)
                logger.debug(f"[WebChannel] Created static directory: {static_dir}")
            except OSError as e:
                logger.debug(f"[WebChannel] Skipped creating static dir (read-only bundle?): {e}")

        urls = _WEB_URLS
        app = build_web_app()

        # 完全禁用web.py的HTTP日志输出
        web.httpserver.LogMiddleware.log = lambda self, status, environ: None

        # 配置web.py的日志级别为ERROR
        logging.getLogger("web").setLevel(logging.ERROR)
        logging.getLogger("web.httpserver").setLevel(logging.ERROR)

        # Build WSGI app with middleware (same as runsimple but without print)
        func = web.httpserver.StaticMiddleware(app.wsgifunc())
        func = web.httpserver.LogMiddleware(func)
        server = web.httpserver.WSGIServer((host, port), func)
        server.daemon_threads = True
        # Default request_queue_size(5) / timeout(10s) / numthreads(10) are
        # too small: when SSE streams occupy many threads, the backlog fills
        # and new connections get refused (ERR_CONNECTION_ABORTED).
        server.request_queue_size = 128
        server.timeout = 300
        server.requests.min = 20
        server.requests.max = 80
        # Allow large attachments (screenshots, PDFs, short videos). cheroot's
        # default is unlimited (0), but pin an explicit, generous cap so an
        # oversized body fails with a clean 413 instead of a connection reset
        # that surfaces in the client as an opaque "Failed to fetch".
        try:
            server.max_request_body_size = 512 * 1024 * 1024  # 512 MB
        except Exception:
            pass
        self._http_server = server
        # Reclaim orphaned SSE logs so disconnected clients don't leak memory.
        self._start_sse_janitor()
        # prepare() binds the socket, serve() runs the accept loop. Splitting
        # start() into the two lets us report a bind failure with the port in
        # hand, and keeps the "console is running" banner honest: it now only
        # prints once we really own the port.
        try:
            server.prepare()
        except OSError as e:
            _log_bind_failure(host, port, e)
            raise
        SERVING.set()
        _log_startup_banner()
        try:
            server.serve()
        except (KeyboardInterrupt, SystemExit):
            server.stop()

    def stop(self):
        if self._http_server:
            try:
                self._http_server.stop()
                logger.info("[WebChannel] HTTP server stopped")
            except Exception as e:
                logger.warning(f"[WebChannel] Error stopping HTTP server: {e}")
            self._http_server = None


_NAVIGATION_MODES = ("classic", "split")


_PREVIEW_SCROLLBAR_CSS = (
    "<style>"
    "html{scrollbar-width:thin;scrollbar-color:rgba(128,128,128,.45) transparent}"
    "::-webkit-scrollbar{width:8px;height:8px}"
    "::-webkit-scrollbar-track{background:transparent}"
    "::-webkit-scrollbar-corner{background:transparent}"
    "::-webkit-scrollbar-thumb{background:rgba(128,128,128,.45);border-radius:4px;"
    "border:2px solid transparent;background-clip:padding-box}"
    "::-webkit-scrollbar-thumb:hover{background:rgba(128,128,128,.7);"
    "background-clip:padding-box}"
    "</style>"
)


_HEAD_OPEN_RE = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)


_HTML_OPEN_RE = re.compile(rb"<html\b[^>]*>", re.IGNORECASE)


def _inject_preview_chrome(raw: bytes) -> bytes:
    """Insert the scrollbar stylesheet into a previewed HTML document."""
    css = _PREVIEW_SCROLLBAR_CSS.encode("utf-8")
    for pattern in (_HEAD_OPEN_RE, _HTML_OPEN_RE):
        m = pattern.search(raw)
        if m:
            return raw[: m.end()] + css + raw[m.end():]
    return css + raw


_HTTP_STATUS_TEXT = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    410: "Gone",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


_SCAN_ERROR_STATUS = {
    "not_owner": 404,
    "expired": 410,
    "invalid_transition": 409,
    "binding_mismatch": 409,
    "terminal": 409,
    "no_provider_result": 409,
    "not_confirmed": 409,
    "no_active_scan": 409,
    "authorization_refused": 403,
    "readback_refused": 403,
    "quota_refused": 403,
    "quota_not_wired": 503,
    "audit_failed": 503,
    "provider_result_crypto": 503,
    "state_not_shared": 503,
    "bad_provider_result": 502,
    "provider_unavailable": 502,
    "secret_in_receipt": 500,
}


def _request_agent_id(source) -> str:
    value = getattr(source, "agent_id", None)
    if value is None:
        value = getattr(source, "agent", None)
    if value is None and isinstance(source, dict):
        value = source.get("agent_id") or source.get("agent")
    # web.py merges query string and form body, so a field present in both
    # arrives as a list. Collapse it to a single id rather than letting an
    # unhashable list reach registry lookups.
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return value or None


def _scoped_agent_id(source) -> str:
    """The Agent a request is scoped to: its payload, or the URL's query string.

    Clients put ``agent_id`` in the query string and deliberately keep it out of
    a multipart body — web.py merges the two, and a field present in both
    arrives as a list that breaks handlers expecting a string (see the console's
    fetch wrapper and the desktop client's ``postFormData``). So a body read on
    its own — ``_raw_web_input()`` is ``rawinput("post")`` — misses the Agent
    unless the query string is read too, and the request quietly answers as the
    *default* Agent instead of the selected one: a recording is written into the
    wrong workspace and an imported document builds the wrong knowledge service.

    Upstream fixed exactly this in its own handlers
    (``channel/web/core/_common.py::_scoped_agent_id``). The fork serves its own
    handlers, so the pattern lives here too, next to ``_request_agent_id``, and
    every body-reading fork route resolves through it.
    """
    agent_id = _request_agent_id(source)
    if agent_id:
        return agent_id
    try:
        from urllib.parse import parse_qs
        query = parse_qs(web.ctx.env.get("QUERY_STRING") or "")
    except Exception:
        return None
    return _request_agent_id(query)


def _skill_service(agent_id: str = ''):
    """
    A SkillService over the skills the console manages.

    Skills stay anchored to the agent's state root even while a session has a
    project open, so this deliberately resolves the workspace without a session.
    ``agent_id`` selects which agent's skills to manage, so a multi-agent setup
    keeps each agent's library isolated.
    """
    from channel.web.web_channel import _get_workspace_root
    from agent.skills.manager import SkillManager
    from agent.skills.service import SkillService
    from common import state_dir
    workspace_root = _get_workspace_root(agent_id=agent_id or None)
    custom_dir = str(state_dir.skills_dir(base=workspace_root))
    return SkillService(SkillManager(custom_dir=custom_dir))


_SCHEDULER_STATUS_LINES = {
    400: "400 Bad Request",
    401: "401 Unauthorized",
    403: "403 Forbidden",
    404: "404 Not Found",
    409: "409 Conflict",
    503: "503 Service Unavailable",
}


def _agent_admin_service():
    from channel.web.web_channel import get_data_root
    from agent.admin import AgentAdminService
    return AgentAdminService(os.path.join(get_data_root(), "config.json"))


def _bind_channel_instance(channel_type: str, instance_id: str = "", agent_id: str = "", members=None):
    """Point one channel instance at an Agent (and team), hot-swapping without a restart.

    The binding lives on the channel instance itself (channel_instances[].agent_id
    in team.json), the single source of truth for routing. For a single-instance
    channel the instance id is just the channel type. An empty agent_id unbinds it
    (falls back to the default Agent).

    Rebinding only changes *which* Agent inbound messages route to — the
    credentials and connection are untouched — so there is no reason to tear
    down and re-establish the IM link. We persist the new binding and then set
    ``bound_agent_id`` live on the running channel; the next inbound message
    reads the updated value. This avoids the reconnect storm a restart caused
    when the user flipped the picker a few times.
    """
    from channel.web.web_channel import conf
    from channel.channel_instances import upsert_instance

    ctype = (channel_type or "").strip().lower()
    if not ctype:
        raise ValueError("channel_type is required")
    target_id = (instance_id or "").strip() or ctype
    agent_id = (agent_id or "").strip()

    inst = upsert_instance(
        conf(),
        channel_type=ctype,
        instance_id=target_id,
        agent_id=agent_id,
        members=members,
    )

    try:
        import sys
        app_module = sys.modules.get("__main__") or sys.modules.get("app")
        mgr = _live_channel_manager()
        channel = mgr.get_channel(target_id) if mgr else None
        if channel is not None:
            # Live-update owner + team on the running instance. Empty owner means
            # "follow the default Agent". No restart: this only changes routing.
            channel.bound_agent_id = agent_id
            channel.members = list(inst.members or [])
            logger.info(
                f"[WebChannel] Channel '{target_id}' rebound to "
                f"'{agent_id or 'default'}' with team {inst.members or []} (no restart)"
            )
    except Exception as e:
        logger.error(
            f"[WebChannel] Failed to hot-rebind channel '{target_id}': {e}",
            exc_info=True,
        )

    return {
        "instance_id": inst.instance_id,
        "agent_id": inst.agent_id,
        "members": list(inst.members or []),
    }


def _reload_agent_runtime(service, changed_agent_ids=None) -> None:
    """Re-point the live runtime at a freshly loaded roster.

    This runs inside the roster-edit request, so it must stay cheap. The old
    implementation tore everything down - stop every scheduler, drop every
    cached session, then rebuild all of them - which grew linearly with the
    number of Agents (each rebuild reloads dozens of skills). Editing one
    Agent's name should not cost a full-fleet reload.

    Instead we reconcile incrementally:
      * swap the registry/router (always cheap),
      * start a scheduler only for Agents that gained one, stop those that
        disappeared, and leave already-running ones untouched,
      * evict only the sessions of the Agents that actually changed, so their
        next turn picks up the new name / model / persona. Everyone else keeps
        their warm cache.

    ``changed_agent_ids`` narrows the session eviction to just the edited
    Agents. When omitted we fall back to evicting nothing extra beyond the
    add/remove diff, since pure metadata edits without an id (e.g. binding
    changes) touch no cached runtime.
    """
    from agent.registry import set_agent_registry
    from agent.routing import AgentRouter, set_agent_router

    settings = service._load()
    registry = service._registry(settings)
    router = AgentRouter.from_config(settings, registry)
    set_agent_registry(registry)
    set_agent_router(router)

    from bridge.bridge import Bridge
    bridge = Bridge()
    agent_bridge = getattr(bridge, "_agent_bridge", None)
    if agent_bridge is None:
        return

    agent_bridge.agent_registry = registry
    agent_bridge.agent_router = router

    # Reconcile schedulers against what is already running, rather than
    # stopping and recreating the whole set.
    from agent.tools.scheduler.integration import init_scheduler, stop_scheduler
    live_ids = {p.id for p in registry.list(include_disabled=False)}
    previously = set(agent_bridge.scheduler_agent_ids)

    for agent_id in previously - live_ids:
        try:
            stop_scheduler(agent_id)
        except Exception as e:
            logger.warning(f"[WebChannel] stop_scheduler({agent_id}) failed: {e}")
        agent_bridge.scheduler_agent_ids.discard(agent_id)

    for profile in registry.list(include_disabled=False):
        if profile.id in previously:
            continue  # already has a running scheduler; init_scheduler is a no-op
        if init_scheduler(agent_bridge, profile.workspace, profile.id):
            agent_bridge.scheduler_agent_ids.add(profile.id)
    agent_bridge.scheduler_initialized = bool(agent_bridge.scheduler_agent_ids)

    # Drop cached runtimes only for the Agents whose definition changed, so the
    # edit takes effect on their next turn without wiping everyone's session.
    for agent_id in (changed_agent_ids or []):
        try:
            agent_bridge.clear_agent(agent_id)
        except Exception as e:
            logger.warning(f"[WebChannel] clear_agent({agent_id}) failed: {e}")


AVATAR_IMAGE_TOKEN = "image"


AVATAR_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


MAX_AVATAR_BYTES = 2 * 1024 * 1024


def _avatar_path(agent_id: str) -> Optional[str]:
    from common.state_dir import shared_root

    base = shared_root() / "avatars"
    for suffix in AVATAR_TYPES:
        candidate = base / f"{agent_id}{suffix}"
        if candidate.is_file():
            return str(candidate)
    return None


def _annotate_sessions_with_projects(store, result: dict, agent_id: Optional[str],
                                    user_id: Optional[str] = None) -> None:
    """Attach each session's project space, and say how to group the list.

    ``group_mode`` is decided here rather than in the browser because the client
    only ever holds one page: whether more than one space is in play is a fact
    about all sessions, not about the fifty currently on screen.

    - ``time``    one space in use (the common case) - group by 今天/昨天/更早,
                  exactly as before projects existed.
    - ``project`` several spaces in use - group by project, so multi-project
                  users can find a conversation by where it belongs.
    """
    from agent.workspace import project_store
    from common.state_dir import state_root_str

    project_map = project_store.get_project_map(agent_id)
    default_workspace = state_root_str()

    for session in result.get("sessions") or []:
        path = project_map.get(session["session_id"])
        session["project"] = (
            {"path": path, "name": project_store.display_name_for(path)}
            if path else None
        )

    # Distinct spaces across every web session, default workspace included as
    # one space when any session is still using it.
    space_paths = set()
    uses_default = False
    for sid in store.list_session_ids(channel_type="web", user_id=user_id):
        path = project_map.get(sid)
        if path:
            space_paths.add(path)
        else:
            uses_default = True

    result["space_count"] = len(space_paths) + (1 if uses_default else 0)
    result["group_mode"] = "project" if result["space_count"] > 1 else "time"
    result["default_workspace"] = default_workspace
    # The user's chosen sidebar order of spaces (project paths + the default
    # sentinel). The client uses it to sort project groups; unspecified spaces
    # fall back after the ordered ones.
    result["project_order"] = project_store.get_order()


def _annotate_coding_sessions(store, result: dict, agent_id: Optional[str]) -> None:
    """Mark the coding rows of a session list with their link state.

    ``agent_type`` in the row's badge already says "this conversation is backed
    by the coding service"; ``sync_state`` says whether the remote session
    actually exists yet, which is what lets the console render a reservation as
    "creation unfinished, retryable" instead of an openable conversation.

    Only the *persistent* half is published. A running/idle answer is a
    transient result of a refresh round (design D3) and is deliberately absent:
    the list must not claim to know something it has not just asked.
    """
    rows = result.get("sessions") or []
    if not rows:
        return
    try:
        states = store.coding_link_states([row.get("session_id") for row in rows])
    except Exception as e:  # noqa: BLE001 - a marker must not fail the list
        logger.debug(f"[WebChannel] Coding state annotation skipped: {e}")
        return
    for row in rows:
        state = states.get(row.get("session_id"))
        if state:
            row["sync_state"] = state


def _agent_badge(profile) -> dict:
    """The per-Agent label the client renders next to a conversation.

    ``agent_type`` is part of the badge, which is what lets the session list
    (and the composer's roster) route a coding conversation to the embedded
    frame without a second request per row. It is exact rather than a hint: a
    coding Agent can only ever hold coding sessions, because every normal
    execution path refuses it before writing a row, and the coding entry always
    writes a link.

    Read through ``getattr`` because this is a *projection*: a profile stand-in
    that predates the field is a normal Agent, and a label must never be the
    thing that fails a list request.
    """
    from agent.registry import AGENT_TYPE_NORMAL

    return {"id": profile.id, "name": profile.name, "avatar": profile.avatar or "",
            "agent_type": getattr(profile, "agent_type", None) or AGENT_TYPE_NORMAL}


def _roster_from_members(host_agent_id: str, members) -> List[dict]:
    """Badge every reachable member of a conversation, host first."""
    from agent.registry import get_agent_registry

    if not members:
        return []
    registry = get_agent_registry()
    roster: List[dict] = []
    for agent_id in [host_agent_id, *members]:
        if any(item["id"] == agent_id for item in roster):
            continue
        try:
            roster.append(_agent_badge(registry.get(agent_id)))
        except Exception:
            continue
    return roster


def _session_roster(session_id: str, host_agent_id: str) -> List[dict]:
    """Everyone who can be addressed in this conversation, host included.

    Empty for a conversation nobody was invited into, which is every
    conversation until the user says otherwise.
    """
    from agent.workspace import session_prefs

    try:
        members = session_prefs.get_prefs(session_id, host_agent_id).get("members")
    except Exception as e:
        logger.debug(f"[WebChannel] roster lookup failed for {session_id}: {e}")
        return []
    return _roster_from_members(host_agent_id, members)


def _addressed_agent_id(text: str, roster: List[dict]) -> str:
    """The teammate this message names, or "" when it names nobody.

    Matching accepts the display name as well as the id, because the composer
    writes the name — nobody types ``@agent-17n3e8`` on purpose. Longer labels
    are tried first so that a name containing another name still resolves to
    the one actually written.

    Only a leading mention counts. Naming somebody mid-sentence is usually
    talking *about* them ("ask Ops to..."), not handing them the turn.
    """
    stripped = (text or "").lstrip()
    if not stripped.startswith("@"):
        return ""
    candidates = []
    for item in roster:
        for label in (item.get("name") or "", item.get("id") or ""):
            if label:
                candidates.append((label, item["id"]))
    for label, agent_id in sorted(candidates, key=lambda pair: -len(pair[0])):
        pattern = r"^@" + re.escape(label) + r"(?=[\s，,：:、]|$)"
        if re.match(pattern, stripped, re.IGNORECASE):
            return agent_id
    return ""


def _as_epoch(value) -> int:
    """Best-effort convert a session timestamp into a sortable epoch int.

    New databases store ``last_active``/``created_at`` as integer Unix
    timestamps, but a workspace carried over from an older build may still hold
    them as ``'YYYY-MM-DD HH:MM:SS'`` strings. Parsing those defensively keeps
    the merged session list from crashing the whole API (which would leave the
    web sidebar empty) just because one legacy row can't be ``int()``-ed.
    """
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    from datetime import datetime

    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return 0


def _list_sessions_across_agents(page: int, page_size: int,
                                 ctx: "Optional[RequestContext]" = None,
                                 q: str = "",
                                 archived: bool = False) -> dict:
    """One page of every Agent's conversations, merged.

    Sessions are stored one database per Agent, so "all conversations" is a
    merge across files rather than a query. Each Agent is asked for as many rows
    as the requested page could possibly draw from it, because any of them can
    supply the row that sorts into that page.

    Presenting them in one list is what keeps a second Agent from feeling like a
    second account: the alternative, switching the whole console to look at
    another Agent's conversations, makes the roster a tenant selector.

    In database mode ``ctx`` limits the merge to the tenant-bound agents and,
    when a user is present, filters each Agent's sessions to that user.

    A title search gathers all matching summaries before deduplication and
    pagination, so duplicates outside an early candidate page cannot inflate
    the result count or leave later pages short. Message bodies are not read.
    """
    from channel.web.web_channel import _tenant_ids_for_context
    from agent.memory import get_conversation_store
    from agent.memory.conversation_store import normalize_session_search_query
    from agent.registry import get_agent_registry
    from agent.workspace import project_store, session_prefs
    from common.state_dir import state_root_str

    q = normalize_session_search_query(q)
    take = max(1, page) * page_size
    merged: List[dict] = []
    total = 0
    space_paths = set()
    uses_default = False
    user_id = ctx.user_id if ctx else None
    visible = _tenant_ids_for_context(ctx)
    try:
        members_index = session_prefs.members_index()
    except Exception as e:
        # Faces are decoration; losing them must not cost the user the list.
        logger.warning(f"[WebChannel] Could not read session rosters: {e}")
        members_index = {}

    for profile in get_agent_registry().list(include_disabled=False):
        if visible is not None and profile.id not in visible:
            continue
        try:
            store = get_conversation_store(profile.workspace)
            if q:
                matches = []
                search_page = 1
                while True:
                    batch = store.list_sessions(
                        channel_type="web", page=search_page, page_size=500,
                        user_id=user_id, q=q, archived=archived,
                    )
                    rows = batch.get("sessions") or []
                    matches.extend(rows)
                    if not batch.get("has_more") or not rows:
                        break
                    search_page += 1
                chunk = {"sessions": matches, "total": len(matches)}
            else:
                chunk = store.list_sessions(channel_type="web", page=1, page_size=take,
                                            user_id=user_id, archived=archived)
            project_map = project_store.get_project_map(profile.id)
            session_ids = store.list_session_ids(channel_type="web", user_id=user_id,
                                                 archived=archived)
        except Exception as e:
            # One unreadable workspace must not blank out the whole list; the
            # other Agents' conversations are still perfectly readable.
            logger.warning(
                f"[WebChannel] Skipping sessions for agent={profile.id}: {e}"
            )
            continue

        total += chunk.get("total", 0)
        # The type comes from the badge and the link state from the store: both
        # markers the console needs, applied to the same dicts the loop below
        # appends to the merged list.
        _annotate_coding_sessions(store, chunk, profile.id)
        badge = _agent_badge(profile)
        for session in chunk.get("sessions") or []:
            path = project_map.get(session["session_id"])
            session["agent"] = badge
            # Only a conversation with more than one Agent in it needs faces in
            # the list; a solo one reads better as a plain row, exactly as it
            # did before there was a roster.
            roster = _roster_from_members(
                profile.id, members_index.get((profile.id, session["session_id"]))
            )
            if len(roster) > 1:
                session["participants"] = roster
            session["project"] = (
                {"path": path, "name": project_store.display_name_for(path)}
                if path else None
            )
            merged.append(session)

        for sid in session_ids:
            path = project_map.get(sid)
            if path:
                space_paths.add(path)
            else:
                uses_default = True

    # One row per conversation. A session id can exist in more than one Agent's
    # store (an older client once let a conversation change hands mid-way, and
    # each side kept the turns it saw); showing it twice makes both rows light
    # up as "selected". Keep the copy holding the bulk of the conversation —
    # that's the one the user recognises — and let the newest break a tie.
    by_id: Dict[str, dict] = {}
    for session in merged:
        sid = session.get("session_id")
        kept = by_id.get(sid)
        if kept is None or (
            (int(session.get("msg_count") or 0), _as_epoch(session.get("last_active")))
            > (int(kept.get("msg_count") or 0), _as_epoch(kept.get("last_active")))
        ):
            by_id[sid] = session
    total -= len(merged) - len(by_id)
    merged = list(by_id.values())

    # Same ordering the per-Agent query applies, so a merged page looks exactly
    # like a single Agent's page does.
    merged.sort(
        key=lambda s: (
            0 if s.get("pinned") else 1,
            -_as_epoch(s.get("last_active")),
        )
    )
    offset = (max(1, page) - 1) * page_size
    result = {
        "sessions": merged[offset:offset + page_size],
        "total": total,
        "page": max(1, page),
        "page_size": page_size,
        "has_more": total > offset + page_size,
        "space_count": len(space_paths) + (1 if uses_default else 0),
        "default_workspace": state_root_str(),
        "project_order": project_store.get_order(),
    }
    result["group_mode"] = "project" if result["space_count"] > 1 else "time"
    return result


def _session_model_catalog() -> List[dict]:
    """Providers a session may switch to, newest-first within each provider.

    Only providers with a credential on file are offered: listing one without an
    API key would let the user pick a model that fails on the next message.
    The globally active provider is always included, even if its key lives in
    the environment rather than in config.json.
    """
    from channel.web.web_channel import ConfigHandler
    from channel.web.web_channel import conf
    local_config = conf()
    active_bot_type = local_config.get("bot_type") or ""
    active_provider = "openai" if active_bot_type == const.CHATGPT else active_bot_type
    if local_config.get("use_linkai") and local_config.get("linkai_api_key"):
        active_provider = "linkai"
    active_model = str(local_config.get("model") or "").strip()

    catalog: List[dict] = []
    for pid, pinfo in ConfigHandler.PROVIDER_MODELS.items():
        if pid == "custom" or not pinfo.get("models"):
            continue
        key_field = pinfo.get("api_key_field")
        has_key = bool(key_field and str(local_config.get(key_field) or "").strip())
        if not has_key and pid != active_provider:
            continue
        models = list(pinfo["models"])
        # The user can pin a custom model name to a built-in provider (via the
        # global config / capability "custom model" field). That model won't be
        # in the preset list, so surface it here for the active provider so the
        # chat picker can both display and re-select it.
        if pid == active_provider and active_model and active_model not in models:
            models.insert(0, active_model)
        catalog.append({
            "id": pid,
            "label": pinfo["label"],
            "models": models,
        })

    # User-defined OpenAI-compatible providers carry their own credentials, so
    # offer any that have a key on file (or are the active provider). Their model
    # list combines the provider's configured default with the globally active
    # model when this custom provider is the one in use — otherwise a custom
    # provider added without a preset model would be unselectable in chat.
    try:
        from models.custom_provider import get_custom_providers
        for cp in get_custom_providers():
            cid = cp.get("id")
            if not cid:
                continue
            pid = f"custom:{cid}"
            is_active = pid == active_provider
            has_key = bool(str(cp.get("api_key") or "").strip())
            if not has_key and not is_active:
                continue
            models = []
            cp_model = str(cp.get("model") or "").strip()
            if cp_model:
                models.append(cp_model)
            if is_active and active_model and active_model not in models:
                models.insert(0, active_model)
            if not models:
                # Nothing concrete to select yet (no default model and not the
                # active provider) — skip rather than render an empty group.
                continue
            name = cp.get("name") or cid
            catalog.append({
                "id": pid,
                "label": {"zh": name, "en": name},
                "models": models,
            })
    except Exception as e:
        logger.debug(f"[WebChannel] custom providers unavailable: {e}")

    return catalog


def _session_settings_state(session_id: str, agent_id: Optional[str]) -> dict:
    """Effective model + permission for a session, and what it can be changed to.

    ``source`` tells the UI whether a value is this conversation's own choice or
    inherited, so it can show "follow global" as a real, selectable state instead
    of silently duplicating the global value onto every session.

    The model resolves the same way the runtime does (see AgentLLMModel.model):
    the conversation's pin, else the owning Agent's own default model, else the
    global config. ``source`` is ``session`` / ``agent`` / ``global`` accordingly,
    and ``agent`` carries the Agent's default when it has one, so a fresh chat
    with a specialist Agent shows the model it will really answer with.
    """
    from channel.web.web_channel import _authorized_model_codes
    from channel.web.web_channel import _current_db_identity
    from channel.web.web_channel import _is_database_identity
    from channel.web.web_channel import _session_model_catalog
    from channel.web.web_channel import conf
    from agent.workspace import session_prefs

    local_config = conf()
    prefs = session_prefs.get_prefs(session_id, agent_id)

    global_bot_type = local_config.get("bot_type") or ""
    global_provider = "openai" if global_bot_type == const.CHATGPT else global_bot_type
    if local_config.get("use_linkai") and local_config.get("linkai_api_key"):
        global_provider = "linkai"
    global_model = local_config.get("model") or ""
    global_permission = permission_global_mode()

    # The default Agent never has a model of its own: it *is* the global choice.
    agent_default = None
    try:
        from agent.registry import get_agent_registry
        registry = get_agent_registry()
        profile = registry.get(agent_id or None, require_enabled=False)
        if profile.id != registry.default_agent_id and profile.model:
            agent_default = {
                "model": profile.model,
                "provider": profile.bot_type or global_provider,
            }
    except Exception as e:
        logger.debug(f"[WebChannel] agent default model unavailable: {e}")

    # Role-default model resolution (spec 5.2): when the conversation has no
    # pin and the owning Agent has no model, a unique role default for the chat
    # capability is a valid "no source" fallback. A role-default *conflict*
    # (two roles pinning chat to different models) is surfaced, not ranked.
    role_default = None
    role_default_state = None
    ident = _current_db_identity()
    if ident:
        try:
            from auth.service import get_identity_service
            svc = get_identity_service()
            resolved = svc.model_defaults_for(ident.user_id, ident.tenant_id, "chat")
            if resolved["status"] == "default":
                role_default = {
                    "model": resolved["model"],
                    "provider": (str(resolved["model"]).split(":", 1)[0])
                    if ":" in str(resolved["model"]) else global_provider,
                }
                role_default_state = "default"
            elif resolved["status"] == "conflict":
                role_default_state = "conflict"
        except Exception:
            role_default_state = None

    if prefs.get("model"):
        effective_model, effective_provider, source = prefs["model"], prefs.get("provider"), "session"
    elif agent_default:
        effective_model, effective_provider, source = agent_default["model"], agent_default["provider"], "agent"
    elif role_default:
        effective_model, effective_provider, source = role_default["model"], role_default["provider"], "role"
    else:
        effective_model, effective_provider, source = global_model, global_provider, "global"

    catalog = _session_model_catalog()
    allowed = _authorized_model_codes()
    selection_required = False
    if allowed is not None:
        # Restrict the offered models to the caller's model.use grant set. Keep
        # the provider groups but drop models the caller may not select, so the
        # picker cannot offer a model the runtime gate would reject.
        filtered = []
        for group in catalog:
            models = [m for m in group.get("models", []) if m in allowed]
            if not models:
                continue
            kept = dict(group)
            kept["models"] = models
            filtered.append(kept)
        catalog = filtered
        # An inherited default (session pin / Agent / role / global) that is not
        # in the grant set must not be reported as the effective model: the
        # runtime gate would reject it. Surface "choose one" instead. A role
        # default is stored as a full ``provider:{pid}:{code}`` resource id, so
        # compare on the trailing code.
        candidate = str(effective_model or "")
        candidate_code = candidate.rsplit(":", 1)[-1] if candidate else ""
        if not allowed or (candidate and candidate_code not in allowed):
            effective_model, effective_provider, source = "", "", "unset"
            selection_required = True

    return {
        "model": {
            "model": effective_model,
            "provider": effective_provider or global_provider,
            "source": source,
            "selection_required": selection_required,
            "global": {"model": global_model, "provider": global_provider},
            "agent": agent_default,
            "role_default": role_default,
            "role_default_state": role_default_state,
            "providers": catalog,
        },
        "permission": (
            {
                # database mode: execution is owned by the caller's role grants
                # (tool.execute + resource grant) and tenant isolation, not a
                # session-level mode. Offer nothing to pin and say so.
                "mode": global_permission,
                "source": "role",
                "global": global_permission,
                "modes": [],
            }
            if _is_database_identity() else
            {
                "mode": (
                    permission_normalize_mode(prefs["permission"], global_permission)
                    if prefs.get("permission") else global_permission
                ),
                "source": "session" if prefs.get("permission") else "global",
                "global": global_permission,
                "modes": list(PERMISSION_MODES),
            }
        ),
        "team": _session_team_state(prefs, agent_id),
    }


def _session_team_state(prefs: dict, agent_id: Optional[str]) -> dict:
    """Who else is on this conversation, and who could be added.

    An archived member is reported but marked unavailable rather than dropped,
    so the roster the user set is what the roster page shows.
    """
    from agent.registry import get_agent_registry

    registry = get_agent_registry()
    owner_id = registry.get(agent_id or None, require_enabled=False).id
    members = []
    for member_id in prefs.get("members") or []:
        try:
            profile = registry.get(member_id, require_enabled=False)
        except Exception:
            members.append({"id": member_id, "name": member_id, "available": False})
            continue
        members.append({
            **_agent_badge(profile),
            "available": profile.enabled and profile.id != owner_id,
        })
    return {
        "owner": _agent_badge(registry.get(owner_id, require_enabled=False)),
        "members": members,
        "candidates": [
            _agent_badge(profile)
            for profile in registry.list(include_disabled=False)
            if profile.id != owner_id
        ],
    }


_LOG_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|apikey|authorization|"
    r"access[_-]?key|refresh[_-]?token|cookie|credential|client[_-]?secret|"
    r"session[_-]?id)\b(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|(?:bearer|basic|token|apikey)\s+[^\s,;]+|[^\s,;]+)"
)


def _workspace_service(session_id: str = None, agent_id: str = None):
    from channel.web.web_channel import _get_workspace_root
    from agent.workspace.service import WorkspaceService
    return WorkspaceService(_get_workspace_root(session_id, agent_id))


_SYSTEM_ASSET_PREFIXES = ("memory/", "memory\\", "knowledge/", "knowledge\\")


_SYSTEM_ASSET_FILES = ("MEMORY.md", "AGENT.md", "USER.md", "RULE.md")


def _is_system_asset_rel(rel_path: str) -> bool:
    """True if a relative path points at a state_root-anchored system asset."""
    p = (rel_path or "").lstrip("./")
    return p in _SYSTEM_ASSET_FILES or p.startswith(_SYSTEM_ASSET_PREFIXES)


def _system_workspace_service():
    from agent.workspace.service import WorkspaceService
    from common.state_dir import state_root_str
    return WorkspaceService(state_root_str())


def _decorate_entry(svc, entry: dict) -> dict:
    """Attach the URLs the frontend needs to preview or download an entry."""
    from channel.web.web_channel import _build_preview_url
    if entry.get("is_dir"):
        return entry
    abs_path = entry.get("abs_path") or os.path.join(svc.root, entry["path"])
    entry["abs_path"] = abs_path
    entry["raw_url"] = f"/api/file?path={quote(abs_path)}"
    entry["preview_url"] = _build_preview_url(abs_path)
    return entry


def _editable_target(raw_path: str, session_id: str = None, agent_id: str = None,
                     ctx=None):
    """
    Locate a file for the preview panel's text editor: (service, rel_path).

    Narrower than `/api/workspace/resolve`, which only has to serve bytes and so
    accepts anything under the configured serve roots. Reading and writing text
    stay inside the session's workspace (its project dir or the tenant shared
    root), with a tenant-scoped state-root fallback for the memory / knowledge /
    persona assets that live in the Agent's workspace even while a project is
    open.

    An absolute path outside both roots is invisible (404): it must never fall
    back to the global default Agent's workspace or an arbitrary host path.
    """
    from channel.web.web_channel import _authorize_db_file_path
    from channel.web.web_channel import _db_path_visible
    from channel.web.web_channel import _workspace_system_service
    svc = _workspace_service(session_id, agent_id)
    system = _workspace_system_service(ctx, svc)
    raw = (raw_path or "").strip()
    expanded = os.path.expanduser(raw)
    if os.path.isabs(expanded):
        real = os.path.realpath(expanded)
        # Second barrier: even if the fallback loop below ever widened, an
        # absolute path must stay inside the caller's tenant roots (platform
        # root only for a platform admin).
        if ctx is not None and getattr(ctx, "tenant_id", None):
            allowed, via = _authorize_db_file_path(ctx, real)
            if not allowed:
                if via == "forbidden":
                    raise web.forbidden()
                raise web.notfound()
        for candidate in (svc, system):
            try:
                return candidate, candidate.to_workspace_rel(real)
            except ValueError:
                continue
        raise web.notfound()
    rel = svc.to_workspace_rel(raw)
    target = svc
    if svc.root != system.root and _is_system_asset_rel(rel) \
            and not os.path.isfile(svc.resolve(rel)):
        target = system
    # Ownership follows the resolved target, never the declared Agent: a private
    # Agent's workspace nested under the tenant shared root must not become
    # readable through a relative path (or via the state-root fallback).
    if ctx is not None and getattr(ctx, "tenant_id", None):
        try:
            visible = _db_path_visible(ctx, target.resolve(rel))
        except ValueError:
            visible = False
        if not visible:
            raise web.notfound()
    return target, rel


def _is_memory_rel(rel_path: str) -> bool:
    """True if a workspace-relative path points at a memory file backed by the
    vector index (so an edit has to be re-embedded, not just written)."""
    p = (rel_path or "").lstrip("./")
    return p == "MEMORY.md" or p.startswith(("memory/", "memory\\"))


def _mark_memory_dirty(agent_id: str = None) -> None:
    """Flag the agent's memory index stale after a console edit to a memory file.

    The index is built from the file contents, so a human edit here must be
    re-embedded the same way an agent's write/edit tool triggers it — otherwise
    semantic search keeps returning the pre-edit text until something else marks
    the store dirty. Best-effort: a failure here must not fail the save.
    """
    try:
        from bridge.bridge import Bridge
        agent = Bridge().get_agent_bridge().get_agent(agent_id=agent_id or None)
        mm = getattr(agent, "memory_manager", None)
        if mm:
            mm.mark_dirty()
    except Exception as e:
        logger.warning(f"[WebChannel] Failed to mark memory index dirty: {e}")


def _project_state(session_id: str, agent_id: str = None) -> dict:
    """Assemble the project picker state: current selection + recents + root."""
    from agent.workspace import project_store
    from common.runtime_identity import RuntimeIdentity, current_identity
    from common import state_dir

    current = project_store.get_project_dir(session_id, agent_id) if session_id else None
    ident = current_identity()
    if ident.user_id and ident.tenant_id:
        # Database mode: resolve against the tenant's trusted shared root (same
        # rule as _get_workspace_root), so the hint points at the caller's
        # tenant root rather than a bare agent id (or host default workspace).
        from auth.service import get_identity_service
        shared = get_identity_service().tenant_shared_root(ident.tenant_id)
        default_workspace = shared or state_dir.state_root_str(ident)
        projects_root = project_store.user_projects_root() or project_store.projects_root()
    else:
        # Legacy mode: default to the Agent this session belongs to so the
        # selector hint matches the file panel's real root in multi-Agent setups.
        default_workspace = state_dir.state_root_str(RuntimeIdentity(agent_id=agent_id))
        projects_root = project_store.projects_root()
    return {
        "current": (
            {"path": current, "name": os.path.basename(current) or current}
            if current else None
        ),
        "default_workspace": default_workspace,
        "projects_root": projects_root,
        "recents": project_store.list_recents(),
    }


_DRIVES_SENTINEL = "__DRIVES__"


