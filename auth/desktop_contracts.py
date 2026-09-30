"""Machine-readable desktop contracts, validated in one place.

Change ``add-desktop-remote-web-workbench``, task 1.3. ``contracts.md`` is prose;
``contracts/desktop/v1.json`` is the same contract as data, and this module is
the only reader of it on the Python side. The desktop client reads the *same*
file (``desktop/src/main/remote/connection.ts``, checked by
``tests/test_desktop_remote_config.cjs``), so a protocol major or a size limit
cannot drift between the two languages: changing it in one place fails the other
side's test.

What it validates:

* the error envelope -- required fields, known code, ``message`` within its byte
  cap;
* the capability metadata -- required shape and the keys that must never appear;
* protocol negotiation -- a required major mismatch is a refusal, an optional one
  disables that feature only;
* workspace relative paths -- the rules from contracts §4 that stop a traversal
  before it reaches a file API;
* command frames -- required fields, the op allow-list, per-op limits and states.

Nothing here authorizes anything. It checks *shape*; the handler still resolves
identity, tenant and resource rights on every request.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT_PATH = os.path.join(_REPO, "contracts", "desktop", "v1.json")

#: Windows device names that must never appear as a path component.
_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + ["com%d" % n for n in range(1, 10)]
    + ["lpt%d" % n for n in range(1, 10)]
)


def load_contract(path: Optional[str] = None) -> Dict[str, Any]:
    """Read the contract document. Raises if it is missing or malformed."""
    with open(path or CONTRACT_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


_CONTRACT = load_contract()

PROTOCOLS: Dict[str, Dict[str, Any]] = _CONTRACT["protocols"]
ERROR_STATUS: Dict[str, int] = _CONTRACT["error_codes"]
LIMITS: Dict[str, Any] = _CONTRACT["limits"]
ORIGINS: Dict[str, Any] = _CONTRACT["origins"]
BRIDGE: Dict[str, Any] = _CONTRACT["bridge"]
GATEWAY: Dict[str, Any] = _CONTRACT["gateway"]
COMMANDS: Dict[str, Any] = _CONTRACT["commands"]
TRANSFERS: Dict[str, Any] = _CONTRACT["transfers"]
#: Phase-1 client-side "save as": the same-origin prefixes the host may take a
#: download from, and the query parameter names that must never appear in one
#: (the guest's cookie is the only credential a download link may carry).
DOWNLOADS: Dict[str, Any] = _CONTRACT["downloads"]
#: The native->Web child bootstrap: endpoint, cookie name, cookie attributes and
#: id minimums. The handler and the desktop client are both checked against it.
WEB_SESSION: Dict[str, Any] = _CONTRACT["web_session"]

_KIND_BY_STATUS = {400: "request", 401: "auth", 403: "permission", 404: "not_found",
                   409: "conflict", 410: "gone", 413: "limit", 422: "content",
                   429: "throttle", 503: "unavailable", 504: "timeout"}


def status_for(code: str) -> Optional[int]:
    """The HTTP status a contract error code must carry, or None if unknown."""
    return ERROR_STATUS.get(code)


def validate_error(payload: Any) -> List[str]:
    """Validate an error envelope; returns human-readable problems (empty = ok)."""
    problems: List[str] = []
    if not isinstance(payload, dict):
        return ["error payload is not an object"]
    if payload.get("status") != "error":
        problems.append("error payload status is not 'error'")
    required = _CONTRACT["error_envelope"]["required"]
    for key in required:
        if payload.get(key) in (None, ""):
            problems.append("error payload is missing %r" % key)
    code = payload.get("code")
    if code is not None and code not in ERROR_STATUS:
        problems.append("unknown error code %r" % code)
    if "retryable" in payload and not isinstance(payload["retryable"], bool):
        problems.append("retryable must be a boolean")
    message = payload.get("message")
    cap = _CONTRACT["error_envelope"]["message_max_bytes"]
    if isinstance(message, str) and len(message.encode("utf-8")) > cap:
        problems.append("message exceeds %d bytes" % cap)
    return problems


def validate_error_status(code: str, status: int) -> List[str]:
    """Check that a code is served with its contractual HTTP status.

    A failure must be a real non-2xx status (contracts §1): wrapping an error in
    HTTP 200 is the specific mistake this catches.
    """
    problems: List[str] = []
    expected = ERROR_STATUS.get(code)
    if expected is None:
        return ["unknown error code %r" % code]
    if status != expected:
        problems.append("code %r must use HTTP %d, got %s" % (code, expected, status))
    if 200 <= status < 300:
        problems.append("error code %r must not be delivered with HTTP 2xx" % code)
    return problems


def validate_meta(data: Any) -> List[str]:
    """Validate the ``GET /api/desktop/meta`` payload (no user data, bounded)."""
    problems: List[str] = []
    if not isinstance(data, dict):
        return ["meta data is not an object"]
    for key in _CONTRACT["meta_envelope"]["required"]:
        if key not in data:
            problems.append("meta data is missing %r" % key)
    for key in _CONTRACT["meta_envelope"]["forbidden_keys"]:
        if key in data:
            problems.append("meta data must not contain %r" % key)
    protocols = data.get("protocols")
    if protocols is not None and not isinstance(protocols, dict):
        problems.append("protocols must be an object")
    if isinstance(protocols, dict):
        for name, version in protocols.items():
            if not isinstance(version, dict) or not isinstance(version.get("major"), int):
                problems.append("protocol %r has no integer major" % name)
    entries = data.get("console_entry_paths")
    if entries is not None:
        if not isinstance(entries, list) or not all(isinstance(p, str) for p in entries):
            problems.append("console_entry_paths must be a list of strings")
        elif any(p in ("/preview", "/uploads", "/api/file") for p in entries):
            offending = [p for p in entries if p in ("/preview", "/uploads", "/api/file")]
            problems.append("console_entry_paths must not list content pages: %s"
                            % ", ".join(offending))
    web = data.get("remote_web")
    if isinstance(web, dict):
        for key in ("implemented", "accepted", "configured", "available"):
            if not isinstance(web.get(key), bool):
                problems.append("remote_web.%s must be a boolean" % key)
        if web.get("available") is False and not web.get("reason"):
            problems.append("an unavailable capability must state a reason")
    elif web is not None:
        problems.append("remote_web must be an object")
    try:
        encoded = json.dumps(data).encode("utf-8")
    except (TypeError, ValueError):
        problems.append("meta data is not JSON-serializable")
        return problems
    cap = _CONTRACT["meta_envelope"]["max_bytes"]
    if len(encoded) > cap:
        problems.append("meta payload exceeds %d bytes" % cap)
    return problems


def negotiate(server_protocols: Any,
              client: Optional[Dict[str, Dict[str, Any]]] = None
              ) -> Tuple[bool, List[str], List[str]]:
    """Negotiate protocol majors.

    Returns ``(ok, disabled, problems)``. A *required* protocol whose major
    differs (or which is absent) refuses the connection; an *optional* one with
    an unknown major is disabled rather than fatal. Unknown protocols the server
    offers but the client does not use are ignored.
    """
    if not isinstance(server_protocols, dict):
        return False, [], ["server protocols are not an object"]
    client = client or PROTOCOLS
    disabled: List[str] = []
    problems: List[str] = []
    for name, spec in client.items():
        server = server_protocols.get(name)
        compatible = isinstance(server, dict) and server.get("major") == spec["major"]
        if compatible:
            continue
        if spec.get("required"):
            problems.append("required protocol %r is not compatible" % name)
            return False, [], problems
        disabled.append(name)
    return True, disabled, problems


def validate_relative_path(path: Any) -> List[str]:
    """Validate a workspace ``relative_path`` (contracts §4).

    Root is expressed by *omitting* the path, never by ``..`` or an absolute
    form. Only ``/`` separates components; no Unicode normalization is applied
    here because it could change the real file name.
    """
    problems: List[str] = []
    if path is None:
        # Omitted path means the authorised root.
        return problems
    if not isinstance(path, str):
        return ["relative_path must be a string"]
    if path == "":
        problems.append("relative_path must not be the empty string")
        return problems
    if "\x00" in path:
        problems.append("relative_path must not contain NUL")
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        problems.append("relative_path must not be absolute")
    if "\\" in path:
        problems.append("relative_path must use '/' as the only separator")
    for component in path.split("/"):
        if component in ("", ".", ".."):
            problems.append("relative_path must not contain an empty, '.' or '..' component")
            break
    for component in path.split("/"):
        stem = component.split(".")[0].lower()
        if stem in _RESERVED_NAMES:
            problems.append("relative_path must not use the reserved name %r" % component)
            break
    return problems


def validate_download_url(raw: Any, *, origin: str) -> List[str]:
    """Validate a same-origin server URL the guest wants saved locally (contracts §5).

    A download is a *server* resource fetched with the guest session's cookie.
    The URL therefore has to be the authorized origin over https, on a declared
    download or document prefix, and free of any credential in the query -- a
    link that carries a token would work outside the paired session, which is
    exactly what the phase-1 boundary forbids.
    """
    from urllib.parse import urlparse, parse_qsl  # local: only this validator needs it

    problems: List[str] = []
    if not isinstance(raw, str) or not raw:
        return ["download URL must be a non-empty string"]
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        problems.append("download URL must use http or https")
    if parsed.username or parsed.password:
        problems.append("download URL must not carry userinfo")
    base = urlparse(origin)
    if (parsed.scheme != base.scheme or parsed.netloc != base.netloc):
        problems.append("download URL must be on the authorized origin")
    allowed = tuple(DOWNLOADS["document_prefixes"]) + tuple(DOWNLOADS["attachment_prefixes"])
    if not any(parsed.path == prefix or parsed.path.startswith(prefix + "/")
               for prefix in allowed):
        problems.append("download URL path is not a declared download or document prefix")
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in {name.lower() for name in DOWNLOADS["forbidden_query_keys"]}:
            problems.append("download URL must not carry %r in the query" % key)
    return problems


def resolve_artifact_ref(ref: Any, *, origin: str) -> Optional[str]:
    """Resolve a server ``artifact_ref`` to a same-origin URL, or None (contracts §5).

    An artifact reference is a *server-relative* path. Anything that is already a
    URL -- absolute, protocol-relative, or with a scheme -- is refused rather
    than normalized, and so is any form that carries a credential in the query:
    the host must never be handed a target it did not derive itself.
    """
    problems: List[str] = []
    if not isinstance(ref, str) or not ref:
        return None
    if ref.startswith("//") or "://" in ref.split("?", 1)[0]:
        return None
    if not ref.startswith("/"):
        return None
    path_part = ref.split("?", 1)[0]
    if any(part in ("", ".", "..") for part in path_part.split("/")[1:]):
        return None
    candidate = origin.rstrip("/") + ref
    if validate_download_url(candidate, origin=origin):
        return None
    return candidate


def validate_command_frame(frame: Any) -> List[str]:
    """Validate a gateway ``command`` frame against the op schema."""
    problems: List[str] = []
    if not isinstance(frame, dict):
        return ["command frame is not an object"]
    for key in ("v", "type", "request_id", "connection_epoch", "binding_id",
                "workspace_id", "grant_version", "op", "params", "deadline",
                "params_sha256"):
        if frame.get(key) in (None, ""):
            problems.append("command frame is missing %r" % key)
    if frame.get("v") != 1:
        problems.append("command frame v must be 1")
    if frame.get("type") != "command":
        problems.append("frame type is not 'command'")
    grant = frame.get("grant_version")
    if not isinstance(grant, int) or isinstance(grant, bool) or grant < 1:
        problems.append("grant_version must be a positive integer")
    op = frame.get("op")
    ops = COMMANDS["ops"]
    if op not in ops:
        problems.append("unknown op %r" % op)
        return problems
    spec = ops[op]
    params = frame.get("params")
    if not isinstance(params, dict):
        problems.append("params must be an object")
        return problems
    allowed = set(spec["params"])
    extra = sorted(set(params) - allowed)
    if extra:
        problems.append("op %r does not accept %s" % (op, ", ".join(extra)))
    if op == "list":
        limit = params.get("limit")
        if limit is not None and (not isinstance(limit, int) or limit > spec["limit_max"]):
            problems.append("list.limit must be <= %d" % spec["limit_max"])
        rel = validate_relative_path(params.get("relative_path"))
        problems.extend("list: %s" % p for p in rel)
    elif op == "read_text":
        limit = params.get("limit")
        if not isinstance(limit, int) or not 0 < limit <= spec["limit_max"]:
            problems.append("read_text.limit must be 1..%d" % spec["limit_max"])
        problems.extend("read_text: %s" % p for p in validate_relative_path(params.get("relative_path")))
    elif op == "search":
        if not isinstance(params.get("query"), str) or not params.get("query"):
            problems.append("search.query must be a non-empty string")
        if params.get("mode") not in spec["modes"]:
            problems.append("search.mode must be one of %s" % spec["modes"])
    elif op == "stat":
        rel = validate_relative_path(params.get("relative_path"))
        if not params.get("relative_path"):
            problems.append("stat.relative_path is required")
        problems.extend("stat: %s" % p for p in rel)
    elif op == "inspect":
        for key in ("processor_id", "source_ref"):
            if not params.get(key):
                problems.append("inspect.%s is required" % key)
    return problems


def validate_frame_size(payload_bytes: int, *, kind: str = "result") -> List[str]:
    """Check a frame against its byte cap (result frames vs chunk requests)."""
    if kind == "chunk":
        cap = GATEWAY["chunk_request_max_bytes"]
    else:
        cap = GATEWAY["result_frame_max_bytes"]
    if payload_bytes > cap:
        return ["frame of %d bytes exceeds the %d byte cap" % (payload_bytes, cap)]
    return []


def validate_command_state(state: str) -> List[str]:
    if state not in COMMANDS["states"]:
        return ["unknown command state %r" % state]
    return []


def validate_transfer_state(state: str) -> List[str]:
    if state not in TRANSFERS["states"] and state not in TRANSFERS["terminal_states"]:
        return ["unknown transfer state %r" % state]
    return []


def bridge_method_allowed(name: str, *, phase: int = 1) -> bool:
    """Whether ``window.desktopHost`` may expose a method in a given phase."""
    if name not in BRIDGE["methods"]:
        return False
    if phase <= 1:
        return name in BRIDGE["phase1_methods"]
    return True
