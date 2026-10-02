"""Run a local project's scripts through the desktop platform launcher.

Change ``align-desktop-project-execution-with-master`` (task 5.1). The one thing
the local backend must not do itself is run a script in the user's directory:
the launcher that confines it (the platform sandbox, the scrubbed environment,
the process-tree budget -- group 4) lives in the desktop main process, and this
process has no way to apply it. So the backend *asks* the launcher instead, over
the loopback endpoint the shell handed it at spawn time:

    COW_DESKTOP_EXECUTOR_URL    http://127.0.0.1:<port>
    COW_DESKTOP_EXECUTOR_TOKEN  per-launch secret, same idea as the
                                registration token the shell already passes

Both are set only by the desktop shell when it starts this backend. A source
run, a server deployment, or a `bash` in an ordinary (non-desktop) session has
neither, and the honest answer there is a refusal: the script did not run, and
it was not forwarded anywhere either. Falling back to running it here would
execute arbitrary code outside the confinement the user's "打开本机项目"
authorization was about, and forwarding it to the server would run it on a
machine the user never authorized.

Nothing here decides *whether* a script may run (that is
``agent.desktop_local.capabilities`` and the launcher's own grant checks); this
module is the transport, plus the environment contract that makes the transport
optional.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from common.log import logger

URL_ENV = "COW_DESKTOP_EXECUTOR_URL"
TOKEN_ENV = "COW_DESKTOP_EXECUTOR_TOKEN"

#: The refusal when there is no launcher to run a script in. Written for the
#: model and for the user: what did not happen, and why it is not simply retried
#: in another place.
REFUSAL_SCRIPT_NOT_ISOLATED = (
    "本机项目里的脚本只能由桌面端的平台隔离启动器执行；当前本机后端没有可用的"
    "启动器（本机执行端未启动或不可达），因此这条命令没有执行，也没有转发到"
    "服务器运行。请重新打开本机项目后再试。"
)

_SCRIPT_PATH = "/desktop-exec/script"
_CAPABILITIES_PATH = "/desktop-exec/capabilities"
#: A script's own budget is passed through; the transport adds room for the
#: round trip so a command that uses its whole budget is not reported as a
#: transport timeout.
_TRANSPORT_GRACE_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ExecutorEndpoint:
    """Where the platform launcher listens, and the secret to reach it."""

    url: str
    token: str

    def url_for(self, path: str) -> str:
        return self.url.rstrip("/") + path


_endpoint_lock = threading.Lock()
_endpoint_cache: Optional[ExecutorEndpoint] = None
_endpoint_loaded = False


def _is_loopback(url: str) -> bool:
    """Whether ``url`` addresses this machine, whatever the host spelling.

    The endpoint is a command-execution surface; refusing a non-loopback host
    here means a misconfigured environment cannot turn the backend into a client
    of somebody else's launcher.
    """
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        host = (parts.hostname or "").strip().lower()
        if parts.scheme not in ("http", "https"):
            return False
        if host in ("localhost", "127.0.0.1", "::1", "[::1]"):
            return True
        return host.startswith("127.")
    except Exception:
        return False


def executor_endpoint() -> Optional[ExecutorEndpoint]:
    """The launcher's endpoint, or ``None`` when this backend has none.

    Read once per process: the shell sets these before spawning the backend, and
    re-reading the environment per call would let one call's environment edit
    become the next call's execution surface.
    """
    global _endpoint_cache, _endpoint_loaded

    with _endpoint_lock:
        if _endpoint_loaded:
            return _endpoint_cache
        _endpoint_loaded = True
        url = str(os.environ.get(URL_ENV) or "").strip()
        token = str(os.environ.get(TOKEN_ENV) or "").strip()
        if not url or not token:
            _endpoint_cache = None
            return None
        if not _is_loopback(url):
            logger.warning(
                "[LocalExecutor] %s is not a loopback origin; scripts stay refused",
                url,
            )
            _endpoint_cache = None
            return None
        _endpoint_cache = ExecutorEndpoint(url=url, token=token)
        return _endpoint_cache


def reset_executor_cache() -> None:
    """Test hook: forget the environment this process read at first use."""
    global _endpoint_cache, _endpoint_loaded

    with _endpoint_lock:
        _endpoint_cache = None
        _endpoint_loaded = False
    with _capability_lock:
        _capability_cache.clear()


_capability_lock = threading.Lock()
_capability_cache: Dict[str, Dict[str, Any]] = {}


def _scope_key(scope: Optional[Dict[str, Any]]) -> str:
    """A stable key for "whose platform answer is this".

    Keyed by the *authorization* rather than by the platform alone, because the
    launcher's answer names the directory a run may write -- two projects must
    not be told about each other's roots.
    """
    if not scope:
        return ""
    parts = [
        str(scope.get("user_id") or ""), str(scope.get("tenant_id") or ""),
        str(scope.get("device_id") or ""), str(scope.get("workspace_id") or ""),
        str(scope.get("binding_id") or ""), str(scope.get("grant_version") or ""),
    ]
    return "|".join(parts)


def _capability_payload(
    scope: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The launcher's capability answer for ``scope``.

    A *positive* answer is remembered (the platform cannot change while this
    backend runs). A negative one deliberately is not: a grant that has not
    appeared yet -- the user has not finished opening the project -- would
    otherwise keep scripts refused after they did.
    """
    endpoint = executor_endpoint()
    if endpoint is None:
        return None, REFUSAL_SCRIPT_NOT_ISOLATED
    key = _scope_key(scope)
    with _capability_lock:
        cached = _capability_cache.get(key)
    if cached is not None:
        return cached, None
    payload, refusal = _post(
        endpoint, _CAPABILITIES_PATH, {"scope": dict(scope or {})}, timeout=10.0)
    if refusal or payload is None:
        return None, REFUSAL_SCRIPT_NOT_ISOLATED
    if payload.get("supported") is not False:
        with _capability_lock:
            _capability_cache[key] = payload
    return payload, None


def executor_available(
    scope: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[str]]:
    """``(available, reason)`` for running a script here and now.

    Availability is the launcher answering *its own* capability probe, not the
    presence of an environment variable: a shell that started an executor on a
    platform it cannot confine must produce a refusal that names the platform's
    reason (task 4.6), not "no executor".
    """
    payload, refusal = _capability_payload(scope)
    if refusal or payload is None:
        return False, REFUSAL_SCRIPT_NOT_ISOLATED
    if payload.get("supported") is False:
        reason = str(payload.get("reason") or "").strip()
        if reason:
            return False, reason
        return False, REFUSAL_SCRIPT_NOT_ISOLATED
    return True, None


def script_capabilities(
    scope: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """``(description, refusal)``: this platform's script semantics, from the launcher."""
    payload, refusal = _capability_payload(scope)
    if refusal or payload is None:
        return None, REFUSAL_SCRIPT_NOT_ISOLATED
    if payload.get("supported") is False:
        return None, str(payload.get("reason") or REFUSAL_SCRIPT_NOT_ISOLATED)
    text = str(payload.get("description") or "").strip()
    if not text:
        return None, REFUSAL_SCRIPT_NOT_ISOLATED
    return text, None


def script_platform(scope: Optional[Dict[str, Any]] = None) -> str:
    """The platform scripts actually run on, as the launcher reports it.

    ``""`` when the launcher cannot say -- which is *not* the same answer as the
    server's own ``sys.platform``, and must not be replaced by it (task 8.7). A
    local run's commands execute on the user's machine, so asserting this
    machine's platform there is simply wrong.
    """
    payload, refusal = _capability_payload(scope)
    if refusal or payload is None:
        return ""
    return str(payload.get("platform") or "").strip()


def run_script(
    *,
    tool_name: str,
    arguments: Dict[str, Any],
    scope: Dict[str, str],
    cwd: str,
    timeout_ms: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(result, refusal)``: run one script tool call under the launcher.

    ``result`` is the tool's own result payload (status/result/display/ext_data)
    exactly as the launcher's worker produced it, so the caller can hand the
    model the real outcome -- including a non-zero exit code -- rather than a
    rewritten summary.
    """
    endpoint = executor_endpoint()
    if endpoint is None:
        return None, REFUSAL_SCRIPT_NOT_ISOLATED
    body: Dict[str, Any] = {
        "tool": str(tool_name),
        "arguments": arguments if isinstance(arguments, dict) else {},
        "scope": dict(scope or {}),
        "cwd": str(cwd or ""),
    }
    if timeout_ms:
        body["timeout_ms"] = int(timeout_ms)
    payload, refusal = _post(
        endpoint, _SCRIPT_PATH, body,
        timeout=_transport_timeout(timeout_ms),
    )
    if refusal or payload is None:
        return None, refusal or REFUSAL_SCRIPT_NOT_ISOLATED
    return payload, None


def _transport_timeout(timeout_ms: Optional[int]) -> float:
    if not timeout_ms:
        return 300.0 + _TRANSPORT_GRACE_SECONDS
    try:
        seconds = float(timeout_ms) / 1000.0
    except (TypeError, ValueError):
        seconds = 300.0
    return max(5.0, seconds + _TRANSPORT_GRACE_SECONDS)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A launcher endpoint never redirects; following one would move a command."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _post(endpoint: ExecutorEndpoint, path: str,
          body: Dict[str, Any], timeout: float) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """One JSON POST to the launcher, with every failure turned into a refusal."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        endpoint.url_for(path),
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {endpoint.token}",
        },
    )
    # No environment proxies: an executor call is a same-machine call, and a
    # proxy would be the one hop that turns it into a network one.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        return None, _refusal_from_http(e)
    except Exception as e:  # noqa: BLE001 - transport failure is a refusal
        logger.info("[LocalExecutor] %s failed: %s", path, e)
        return None, REFUSAL_SCRIPT_NOT_ISOLATED
    if len(raw) > _MAX_RESPONSE_BYTES:
        return None, REFUSAL_SCRIPT_NOT_ISOLATED
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, REFUSAL_SCRIPT_NOT_ISOLATED
    if not isinstance(payload, dict):
        return None, REFUSAL_SCRIPT_NOT_ISOLATED
    return payload, None


def _refusal_from_http(error: urllib.error.HTTPError) -> str:
    """The launcher's own refusal text, so the model reads the real reason."""
    try:
        raw = error.read(_MAX_RESPONSE_BYTES)
        payload = json.loads(raw.decode("utf-8"))
        message = str(payload.get("message") or "").strip()
        if message:
            return message
    except Exception:
        pass
    return REFUSAL_SCRIPT_NOT_ISOLATED


def script_scope(identity: Any, target: Any,
                 skill_roots: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """The identifiers a launcher call is keyed by: never the project path.

    The launcher resolves the project directory from its own grant table (the
    picked root lives in the desktop main process, not here), so the request
    carries only what identifies the authorization.

    ``skill_roots`` is the one exception, and a deliberate one (task 8.8): this
    run's pinned skill versions live in a cache the *backend* owns, so unlike the
    project directory the shell has no way to know them. They are still not
    trusted -- the launcher accepts a root only when it resolves inside the
    skill-cache root the shell itself was configured with, and refuses the call
    otherwise. Naming a directory the shell already owns under a trusted anchor is
    a different act from letting the request name an arbitrary path.
    """
    scope: Dict[str, Any] = {
        "user_id": str(getattr(identity, "user_id", "") or ""),
        "tenant_id": str(getattr(identity, "tenant_id", "") or ""),
        "device_id": str(getattr(target, "device_id", "") or ""),
        "workspace_id": str(getattr(target, "workspace_id", "") or ""),
        "binding_id": str(getattr(target, "binding_id", "") or ""),
        "grant_version": str(getattr(target, "grant_version", 0) or 0),
        "project_mode": str(getattr(target, "project_mode", "") or ""),
    }
    roots = [str(root) for root in (skill_roots or ()) if str(root or "").strip()]
    if roots:
        # Sorted and de-duplicated so the same pin set produces the same request
        # (and therefore reuses one worker) regardless of deploy order.
        scope["skill_roots"] = sorted(set(roots))
    return scope
