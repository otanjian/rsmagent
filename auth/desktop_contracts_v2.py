"""The v2 (project execution) desktop contract, read and checked in one place.

Change ``align-desktop-project-execution-with-master``, task 6.1. The prose is
``execution-contract.md``; ``contracts/desktop/v2.json`` is the same contract as
data, and this module is the only reader of it on the Python side. The desktop
client reads the *same* file through
``desktop/src/main/project-execution/contract.ts`` (checked by
``tests/test_desktop_execution_contract.cjs``), so a phase name or a limit
cannot drift between the two languages.

v1 is deliberately untouched: ``auth.desktop_contracts`` still reads
``v1.json``, still validates the read-only ``command``/``op`` frames, and this
module never redefines a v1 key. What it *adds*:

* protocol negotiation for ``project_execution`` -- an absent v2 on either side
  makes the new entry point unavailable; it is never downgraded into v1's
  ``inspect``/``materialize``;
* the ``execute_tool`` envelope -- required fields, the tool allow-list, the
  frozen ``tool_schema_version``, the fields that must never appear (a cwd, a
  module path, a credential);
* phases and effects -- the visible phase derived from the existing command
  state (never a new state string written into the database);
* result/artifact/start-permit/journal shapes;
* the meta ``project_execution`` capability block.

Nothing here authorizes anything. These are shape checks; the command service
still resolves identity, tenant, binding, grant version, approval and quota on
every request.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from auth import desktop_contracts as _v1

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT_PATH = os.path.join(_REPO, "contracts", "desktop", "v2.json")


def load_contract(path: Optional[str] = None) -> Dict[str, Any]:
    """Read the v2 contract document. Raises if it is missing or malformed."""
    with open(path or CONTRACT_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


CONTRACT = load_contract()

PROTOCOL: Dict[str, Any] = CONTRACT["protocol"]
PLATFORMS: Dict[str, Any] = CONTRACT["platforms"]
TOOLS: Dict[str, Any] = CONTRACT["tools"]
FRAMES: Dict[str, Any] = CONTRACT["frames"]
PHASES: Dict[str, Any] = CONTRACT["phases"]
EFFECTS: Dict[str, Any] = CONTRACT["effects"]
#: Codes v2 *adds*. v1's ``error_codes`` stay the source for codes v1 defines.
#: The trailing ``note`` key is documentation, not a code.
ERROR_CODES: Dict[str, int] = {
    code: status for code, status in CONTRACT["error_codes"].items() if code != "note"
}
_V2_LIMITS: Dict[str, Any] = CONTRACT["limits"]
ARTIFACT: Dict[str, Any] = CONTRACT["artifact"]
START_PERMIT: Dict[str, Any] = CONTRACT["start_permit"]
JOURNAL: Dict[str, Any] = CONTRACT["journal"]
BROKER: Dict[str, Any] = CONTRACT["broker"]
CAPABILITY: Dict[str, Any] = CONTRACT["capability"]

PROTOCOL_MAJOR = PROTOCOL["major"]
PROTOCOL_NAME = PROTOCOL["name"]
TOOL_SCHEMA_VERSION = TOOLS["schema_version"]

#: Peak script runtime, in seconds, before the tool must be asked for a
#: background handle. Read from the contract so the launcher and the model-facing
#: tool description cannot disagree.
SCRIPT_TIMEOUT_DEFAULT = _V2_LIMITS["script_timeout_default_seconds"]
SCRIPT_TIMEOUT_MAX = _V2_LIMITS["script_timeout_max_seconds"]

_REUSED_FROM_V1 = tuple(_V2_LIMITS["reuse_v1"])

#: Limits v2 declares but the current launcher does **not** enforce. They stay in
#: the contract as the intended bound, and are deliberately withheld from the
#: capability block's advertised ``limits`` (see ``execution_capability``): a
#: client reads that block as "these are the caps that will be applied", and
#: advertising one that nothing applies is the same mistake as offering a script
#: tool on a platform with no launcher. Task 1.7 found ``worker_memory_bytes`` in
#: this state; A17's resource-limit dimension is still open, so it must not be
#: advertised as a cap that will be enforced.
ADVISORY_LIMITS: tuple = tuple(_V2_LIMITS.get("advisory") or ())


#: Keys in ``limits`` that are documentation or a classification, not a bound.
_LIMIT_DOC_KEYS = ("note", "reuse_v1", "advisory", "advisory_note")


def effective_limits() -> Dict[str, Any]:
    """v1's limits plus v2's additions, with no key defined twice.

    The contract names every inherited key in ``limits.reuse_v1`` and refuses to
    restate it, so a bound cannot be loosened in one document and tightened in
    the other. ``contract_problems()`` fails the build if that ever happens.
    """
    merged = dict(_v1.LIMITS)
    for key, value in _V2_LIMITS.items():
        if key in _LIMIT_DOC_KEYS:
            continue
        merged[key] = value
    return merged


LIMITS: Dict[str, Any] = effective_limits()


def status_for(code: str) -> Optional[int]:
    """The HTTP status an error code must carry, from either contract.

    v1 wins for the codes it already defines; a code that appears in both with
    different statuses is a contract bug (``contract_problems()`` reports it).
    """
    if code in _v1.ERROR_STATUS:
        return _v1.ERROR_STATUS[code]
    return ERROR_CODES.get(code)


def validate_error_status(code: str, status: int) -> List[str]:
    """Check a v2 code (or a shared v1 code) is served with its real status.

    v2 adds codes *beside* v1's; the HTTP-2xx-for-an-error mistake that
    ``desktop_contracts.validate_error_status`` catches for v1 has to be caught
    here too, or a new code could be delivered as a success.
    """
    expected = status_for(code)
    if expected is None:
        return ["unknown error code %r" % code]
    problems: List[str] = []
    if status != expected:
        problems.append("code %r must use HTTP %d, got %s" % (code, expected, status))
    if 200 <= status < 300:
        problems.append("error code %r must not be delivered with HTTP 2xx" % code)
    return problems


def contract_problems() -> List[str]:
    """Cross-document consistency, checked by tests rather than at import time."""
    problems: List[str] = []
    for key in _REUSED_FROM_V1:
        if key not in _v1.LIMITS:
            problems.append("limits.reuse_v1 names %r, which v1 does not define" % key)
        if key in _V2_LIMITS:
            problems.append("limit %r is restated here and reused from v1" % key)
    for code, status in ERROR_CODES.items():
        if code in _v1.ERROR_STATUS:
            if _v1.ERROR_STATUS[code] != status:
                problems.append(
                    "error code %r is %d in v1 and %d here"
                    % (code, _v1.ERROR_STATUS[code], status))
        if not 400 <= status < 600:
            problems.append("error code %r has a non-error status %d" % (code, status))
    for state in _v1.COMMANDS["states"]:
        if state not in PHASES["from_state"]:
            problems.append("command state %r has no v2 phase" % state)
    for phase in PHASES["names"]:
        if phase not in PHASES["terminal"] and phase not in PHASES["from_state"].values() \
                and phase != "cancelling":
            problems.append("phase %r is never reachable" % phase)
    for phase in PHASES["terminal"]:
        if phase not in EFFECTS["by_phase"] and phase not in ("succeeded", "failed",
                                                              "outcome_unknown"):
            problems.append("terminal phase %r has no default effect" % phase)
    for code in EFFECTS["by_error_code"]:
        if status_for(code) is None:
            problems.append("effects.by_error_code names unknown code %r" % code)
    for tool in TOOLS["required"]:
        if tool in TOOLS["readonly"] and tool in TOOLS["effectful"]:
            problems.append("tool %r is both readonly and effectful" % tool)
    for tool in TOOLS["effectful"] + TOOLS["readonly"]:
        if tool not in TOOLS["required"]:
            problems.append("tool %r is not in tools.required" % tool)
    for tool in TOOLS["script_tools"]:
        if tool not in TOOLS["effectful"]:
            problems.append("script tool %r must be effectful" % tool)
    for key in BROKER["paths"]:
        if not BROKER["paths"][key].startswith("/api/desktop/execution/"):
            problems.append("broker path %r is outside /api/desktop/execution/" % key)
    if set(BROKER["methods"]) != set(BROKER["paths"]):
        problems.append("every broker path needs exactly one method")
    for name, spec in PLATFORMS.items():
        if name == "note":
            continue
        if not isinstance(spec, dict) or not isinstance(spec.get("supported"), bool):
            problems.append("platform %r has no supported flag" % name)
        elif spec["supported"] and not spec.get("launcher"):
            problems.append("supported platform %r names no launcher" % name)
        elif not spec["supported"] and spec.get("launcher"):
            problems.append("unsupported platform %r must not name a launcher" % name)
    if not platform_supported("posix"):
        problems.append("posix must be a supported platform to run the acceptance suites")
    for kind in ARTIFACT["kinds"]:
        if not isinstance(kind, str) or not kind:
            problems.append("artifact kind %r is not a name" % (kind,))
    return problems


# ---------------------------------------------------------------------------
# Negotiation
# ---------------------------------------------------------------------------

def negotiate(server_protocols: Any) -> Tuple[bool, str]:
    """Whether a v2 client may use the execution entry point against a server.

    Returns ``(available, reason)``. A server that does not offer
    ``project_execution`` at major 2 -- an older server, or a deployment that
    never enabled it -- makes the entry point *unavailable*: refusing is the
    whole point of a separate protocol major, and the caller must not fall back
    to v1's ``inspect``/``materialize`` for an operation that would write.
    """
    if not isinstance(server_protocols, dict):
        return False, "not_implemented"
    offered = server_protocols.get(PROTOCOL_NAME)
    if not isinstance(offered, dict):
        return False, "not_implemented"
    if offered.get("major") != PROTOCOL_MAJOR:
        return False, "protocol_incompatible"
    return True, "available"


def validate_protocol_document(protocols: Any) -> List[str]:
    """Validate a meta/hello ``protocols`` map that claims to include v2."""
    problems: List[str] = []
    if not isinstance(protocols, dict):
        return ["protocols is not an object"]
    for name in (PROTOCOL_NAME,):
        spec = protocols.get(name)
        if spec is None:
            continue
        if not isinstance(spec, dict):
            problems.append("protocol %r has no version object" % name)
            continue
        if not isinstance(spec.get("major"), int) or isinstance(spec.get("major"), bool):
            problems.append("protocol %r has no integer major" % name)
        if spec.get("major") != PROTOCOL_MAJOR:
            problems.append("protocol %r major must be %d" % (name, PROTOCOL_MAJOR))
        if spec.get("required") is not False:
            problems.append(
                "protocol %r must stay optional so a v1-only peer keeps working" % name)
    return problems


# ---------------------------------------------------------------------------
# execute_tool
# ---------------------------------------------------------------------------

def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def tool_supported_on(tool: str, platform: str) -> bool:
    """Whether this build's v2 client may run ``tool`` on ``platform``.

    A platform with no accepted launcher supports nothing: the refusal is
    ``platform_unsupported`` rather than a translated command, because a POSIX
    command string executed by a Windows shell is not the command the user
    approved. On a supported platform, a script tool is only offered where the
    contract describes its syntax -- ``bash`` on ``win32`` stays unsupported even
    once a launcher exists, until its syntax is described here.
    """
    if not platform_supported(platform):
        return False
    if platform == "posix":
        return tool in TOOLS["required"]
    if platform == "win32":
        return tool in TOOLS["required"] and tool not in TOOLS["script_tools"]
    return False


def platform_supported(platform: str) -> bool:
    """Whether the contract declares an accepted launcher for ``platform``."""
    spec = PLATFORMS.get(platform)
    return bool(isinstance(spec, dict) and spec.get("supported") is True)


def validate_execute_frame(frame: Any, *, platform: str = "posix") -> List[str]:
    """Validate an ``execute_tool`` frame (execution-contract §2).

    Checks shape only: identity, ownership and eligibility are re-derived by the
    command service and the broker. Refusals are the specific mistakes that
    would otherwise reach the project directory -- a caller-chosen cwd, a module
    path instead of a tool name, a credential in the arguments, an unknown tool
    or an unbounded timeout.
    """
    problems: List[str] = []
    if not isinstance(frame, dict):
        return ["execute_tool frame is not an object"]
    spec = FRAMES["execute_tool"]
    if frame.get("type") != "execute_tool":
        problems.append("frame type is not 'execute_tool'")
    major = frame.get("protocol_major")
    if not isinstance(major, int) or isinstance(major, bool):
        problems.append("protocol_major must be an integer")
    elif major != PROTOCOL_MAJOR:
        problems.append("protocol_major must be %d" % PROTOCOL_MAJOR)
    for key in spec["required"]:
        if frame.get(key) in (None, ""):
            problems.append("execute_tool is missing %r" % key)
    for key in spec["forbidden"]:
        if key in frame:
            problems.append("execute_tool must not carry %r" % key)

    if not _positive_int(frame.get("grant_version")):
        problems.append("grant_version must be a positive integer")
    if frame.get("selection_generation") is not None \
            and not _non_negative_int(frame.get("selection_generation")):
        problems.append("selection_generation must be a non-negative integer")

    tool = frame.get("tool")
    if tool is not None and tool not in TOOLS["required"]:
        problems.append("unknown tool %r" % tool)
        return problems
    if isinstance(tool, str) and not tool_supported_on(tool, platform):
        problems.append("tool %r is unsupported on platform %r" % (tool, platform))

    version = frame.get("tool_schema_version")
    if version is not None and version != TOOL_SCHEMA_VERSION:
        problems.append("tool_schema_version must be %d" % TOOL_SCHEMA_VERSION)

    arguments = frame.get("arguments")
    if arguments is not None:
        if not isinstance(arguments, dict):
            problems.append("arguments must be an object")
        else:
            for key in spec["forbidden"]:
                if key in arguments:
                    problems.append("execute_tool.arguments must not carry %r" % key)
            problems.extend(_validate_arguments(tool, arguments))

    digest = frame.get("params_digest")
    if digest is not None and not _looks_like_digest(digest):
        problems.append("params_digest must be a sha256: prefixed digest")

    resources = frame.get("skill_resources")
    if resources is not None:
        if not isinstance(resources, list):
            problems.append("skill_resources must be a list")
        else:
            for entry in resources:
                if not isinstance(entry, dict) or not entry.get("skill_id") \
                        or not entry.get("digest"):
                    problems.append("each skill_resource needs skill_id and digest")
                    break
    return problems


def _looks_like_digest(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    body = value[len("sha256:"):]
    return len(body) >= 16 and all(c in "0123456789abcdef" for c in body.lower())


def _non_empty_text(value: Any) -> bool:
    """Whether a value is a string with something in it (the master's rule)."""
    return isinstance(value, str) and bool(value.strip())


def _validate_arguments(tool: Any, arguments: Dict[str, Any]) -> List[str]:
    """The frozen per-tool argument shape (``tool_schema_version`` 1).

    Kept to the fields that must be bounded for safety -- a script's command and
    timeout -- rather than a copy of the master JSON schema, which stays the
    tool's own single source of truth.
    """
    problems: List[str] = []
    if tool in TOOLS["script_tools"]:
        command = arguments.get("command")
        # The master script tool has three executable shapes and a v2 frame must
        # be able to carry each of them: a ``command`` to run, a ``bash_id`` to
        # read the output of a background job, and a ``bash_id`` plus ``kill`` to
        # stop one. A frame with *neither* says nothing about what to do (and a
        # blank command is not a command), which is what stays refused -- the
        # handle itself is scoped and re-checked by the server, never trusted
        # because the device was told about it.
        if not _non_empty_text(arguments.get("bash_id")) \
                and not _non_empty_text(command):
            problems.append("%s.arguments.command must be a non-empty string" % tool)
        timeout = arguments.get("timeout")
        if timeout is not None:
            if not _positive_int(timeout):
                problems.append("%s.arguments.timeout must be a positive integer" % tool)
            elif timeout > SCRIPT_TIMEOUT_MAX:
                problems.append("%s.arguments.timeout must be <= %d seconds"
                                % (tool, SCRIPT_TIMEOUT_MAX))
    return problems


def validate_result_frame(frame: Any) -> List[str]:
    """Validate an ``execution_result`` frame (execution-contract §3, §4).

    A v2 result always carries a *phase*, an *effects* claim and both
    timestamps: the server has to be able to tell "finished and wrote files"
    from "stopped, some files may be partial", which the v1 result shape cannot
    express.
    """
    problems: List[str] = []
    if not isinstance(frame, dict):
        return ["execution_result frame is not an object"]
    if frame.get("type") != "execution_result":
        problems.append("frame type is not 'execution_result'")
    if frame.get("protocol_major") != PROTOCOL_MAJOR:
        problems.append("protocol_major must be %d" % PROTOCOL_MAJOR)
    for key in FRAMES["execution_result"]["required"]:
        if frame.get(key) in (None, ""):
            problems.append("execution_result is missing %r" % key)
    if frame.get("state") not in _v1.COMMANDS["states"]:
        problems.append("unknown command state %r" % frame.get("state"))
    if frame.get("execution_phase") not in PHASES["names"]:
        problems.append("unknown phase %r" % frame.get("execution_phase"))
    if frame.get("effects") not in EFFECTS["names"]:
        problems.append("unknown effects value %r" % frame.get("effects"))
    code = frame.get("error_code")
    if code is not None and status_for(code) is None:
        problems.append("unknown error code %r" % code)
    stdout_cap = FRAMES["execution_result"]["stdout_max_bytes"]
    stderr_cap = FRAMES["execution_result"]["stderr_max_bytes"]
    for key, cap in (("stdout", stdout_cap), ("stderr", stderr_cap)):
        text = frame.get(key)
        if isinstance(text, str) and len(text.encode("utf-8")) > cap:
            problems.append("execution_result.%s exceeds %d bytes in a frame" % (key, cap))
    artifacts = frame.get("artifacts")
    if artifacts is not None:
        if not isinstance(artifacts, list):
            problems.append("artifacts must be a list")
        else:
            for entry in artifacts:
                for problem in validate_artifact(entry,
                                                 workspace_id=frame.get("workspace_id")):
                    problems.append("artifact: %s" % problem)
    # The tool's own payload (a file tool's answer). Checked but never rewritten:
    # the server hands the execution end's real result to the model, so a shape it
    # cannot read has to be a refusal here rather than a fabricated summary.
    result = frame.get("result")
    if result is not None:
        if not isinstance(result, dict):
            problems.append("execution_result.result must be an object")
        else:
            status = result.get("status")
            allowed = FRAMES["execution_result"].get("result_status_values") or []
            if status is not None and allowed and status not in allowed:
                problems.append("execution_result.result.status %r is not a tool status"
                                % status)
    return problems


def validate_heartbeat_frame(frame: Any) -> List[str]:
    problems: List[str] = []
    if not isinstance(frame, dict):
        return ["execution_heartbeat frame is not an object"]
    if frame.get("type") != "execution_heartbeat":
        problems.append("frame type is not 'execution_heartbeat'")
    if frame.get("protocol_major") != PROTOCOL_MAJOR:
        problems.append("protocol_major must be %d" % PROTOCOL_MAJOR)
    for key in FRAMES["execution_heartbeat"]["required"]:
        if frame.get(key) in (None, ""):
            problems.append("execution_heartbeat is missing %r" % key)
    if frame.get("output_bytes") is not None \
            and not _non_negative_int(frame.get("output_bytes")):
        problems.append("output_bytes must be a non-negative integer")
    phase = frame.get("phase")
    if phase is not None and phase not in PHASES["names"]:
        problems.append("unknown phase %r" % phase)
    return problems


# ---------------------------------------------------------------------------
# Phases and effects
# ---------------------------------------------------------------------------

def phase_for(state: str, *, cancel_requested: bool = False,
              error_code: Optional[str] = None) -> Optional[str]:
    """The visible v2 phase for an existing command row, or ``None`` if unknown.

    The database keeps v1's states; the phase is a *projection* computed here so
    a v2 client can see ``cancelling`` (requested, process tree not yet confirmed
    dead) and ``outcome_unknown`` (may have written files, no reliable
    conclusion) without either being written into the state enum.
    """
    base = PHASES["from_state"].get(state)
    if base is None:
        return None
    if cancel_requested and base in PHASES["cancelling_overrides"]:
        return "cancelling"
    if state == "failed" and error_code == PHASES["outcome_unknown_code"]:
        return "outcome_unknown"
    return base


def effects_for(phase: str, *, error_code: Optional[str] = None) -> Optional[str]:
    """What a terminal phase may claim about the project directory.

    The default is deliberately not optimistic: a failed run with no specific
    code reports ``unknown``, and a cancelled run never claims to have rolled
    anything back.
    """
    if error_code is not None and error_code in EFFECTS["by_error_code"]:
        return EFFECTS["by_error_code"][error_code]
    if phase in EFFECTS["by_phase"]:
        return EFFECTS["by_phase"][phase]
    if phase == "succeeded":
        return "completed"
    if phase in ("failed", "outcome_unknown"):
        return "unknown"
    return "none" if phase not in PHASES["terminal"] else None


def is_terminal_phase(phase: str) -> bool:
    return phase in PHASES["terminal"]


def next_command_state(phase: str) -> Optional[str]:
    """The v1 command state a phase must be persisted as, or ``None``.

    ``cancelling`` is not a state: it is ``running`` with a cancellation
    requested, so a persisted row never carries a string the database's state
    enum does not know.
    """
    if phase == "cancelling":
        return "running"
    if phase in _v1.COMMANDS["states"]:
        return phase
    return None


# ---------------------------------------------------------------------------
# Artifacts, start permits, journal
# ---------------------------------------------------------------------------

def validate_artifact(payload: Any, *, workspace_id: Optional[str] = None) -> List[str]:
    """Validate a local artifact reference (execution-contract §6).

    ``source_version`` and the identifiers are required because the client's real
    path must never be the identity of the thing: a preview re-reads the file and
    compares this version, so "same name" is never mistaken for "same contents".
    """
    problems: List[str] = []
    if not isinstance(payload, dict):
        return ["artifact is not an object"]
    for key in ARTIFACT["required"]:
        if payload.get(key) in (None, ""):
            problems.append("artifact is missing %r" % key)
    if payload.get("source") not in (None, ARTIFACT["source_value"]):
        problems.append("artifact.source must be %r" % ARTIFACT["source_value"])
    kind = payload.get("kind")
    if kind is not None and kind not in ARTIFACT["kinds"]:
        problems.append("unknown artifact kind %r" % kind)
    rel = payload.get("relative_path")
    if isinstance(rel, str):
        problems.extend(validate_relative_path(rel))
    size = payload.get("size")
    if size is not None and not _non_negative_int(size):
        problems.append("artifact.size must be a non-negative integer")
    if workspace_id is not None and payload.get("workspace_id") is not None \
            and payload.get("workspace_id") != workspace_id:
        problems.append("artifact.workspace_id does not match the execution workspace")
    if isinstance(rel, str) and (rel.startswith("/") or (len(rel) > 1 and rel[1] == ":")):
        problems.append("artifact.relative_path must not be absolute")
    return problems


def validate_relative_path(path: Any) -> List[str]:
    """A workspace-relative path, using v1's rules (one traversal check only)."""
    return _v1.validate_relative_path(path)


def validate_start_permit(permit: Any, *, now: Optional[int] = None) -> List[str]:
    """Validate a start permit (execution-contract §3).

    Shape plus single-use and expiry: a permit that is already stale must be
    refused *before* the worker is started, never re-used.
    """
    problems: List[str] = []
    if not isinstance(permit, dict):
        return ["start permit is not an object"]
    for key in START_PERMIT["required"]:
        if permit.get(key) in (None, ""):
            problems.append("start permit is missing %r" % key)
    if permit.get("expires_at") is not None and not _positive_int(permit.get("expires_at")):
        problems.append("permit.expires_at must be a positive unix time")
    if permit.get("grant_version") is not None and not _positive_int(permit.get("grant_version")):
        problems.append("permit.grant_version must be a positive integer")
    digest = permit.get("params_digest")
    if digest is not None and not _looks_like_digest(digest):
        problems.append("permit.params_digest must be a sha256: prefixed digest")
    if now is not None and isinstance(permit.get("expires_at"), int) \
            and permit["expires_at"] <= now:
        problems.append("permit is expired")
    ttl = LIMITS["start_permit_ttl_seconds"]
    if isinstance(permit.get("expires_at"), int) and isinstance(permit.get("server_time"), int):
        if permit["expires_at"] - permit["server_time"] > ttl:
            problems.append("permit lifetime exceeds %d seconds" % ttl)
    return problems


def validate_journal_entry(entry: Any) -> List[str]:
    """Validate a local start-journal record (execution-contract §4).

    The journal is written *before* the worker can touch the project: it is what
    makes "started but no completion" a readable fact instead of a silence.
    """
    problems: List[str] = []
    if not isinstance(entry, dict):
        return ["journal entry is not an object"]
    for key in JOURNAL["required"]:
        if entry.get(key) in (None, ""):
            problems.append("journal entry is missing %r" % key)
    digest = entry.get("params_digest")
    if digest is not None and not _looks_like_digest(digest):
        problems.append("journal.params_digest must be a sha256: prefixed digest")
    if entry.get("grant_version") is not None and not _positive_int(entry.get("grant_version")):
        problems.append("journal.grant_version must be a positive integer")
    return problems


def dedup_conflict(existing_digest: Optional[str], incoming_digest: Optional[str]) -> bool:
    """Whether a redelivery of the same command id is a conflict.

    Same id with a different digest is ``command_conflict`` -- the one case that
    must never be silently executed as the new payload.
    """
    return bool(existing_digest) and bool(incoming_digest) \
        and existing_digest != incoming_digest


# ---------------------------------------------------------------------------
# Meta capability block
# ---------------------------------------------------------------------------

def execution_capability(*, platform: str = "posix",
                         tools: Optional[List[str]] = None,
                         available: bool = False,
                         reason: str = "not_implemented",
                         files_write_verified: bool = False,
                         scripts_verified: bool = False,
                         runtime: str = "") -> Dict[str, Any]:
    """The optional ``project_execution`` block for meta/hello.

    ``available`` is not decided here: the caller passes the composed
    capability/switch state. What this function guarantees is the *shape* -- and
    that ``scripts_verified``/``files_write_verified`` cannot be claimed for a
    platform whose tool set does not include them, so the block never advertises
    an enforcement this build does not have.
    """
    supported = [t for t in TOOLS["required"] if tool_supported_on(t, platform)]
    if tools is not None:
        supported = [t for t in supported if t in tools]
    if available and not supported:
        # Nothing runnable here: report the platform, not a capability the
        # launch path would refuse a moment later.
        available, reason = False, "platform_unsupported"
    if available:
        # "available" is the only reason that may accompany an open capability;
        # a closed one keeps the caller's more specific explanation.
        reason = "available"
    scripts = scripts_verified and any(t in TOOLS["script_tools"] for t in supported)
    return {
        "available": bool(available),
        "reason": reason,
        "protocol_major": PROTOCOL_MAJOR,
        "protocol_minor": PROTOCOL["minor"],
        "required": False,
        "tools": supported,
        "tool_schema_version": TOOL_SCHEMA_VERSION,
        "platform": platform,
        "runtime": runtime,
        "files_write_verified": bool(files_write_verified),
        "scripts_verified": bool(scripts),
        "limits": {
            "script_timeout_default_seconds": SCRIPT_TIMEOUT_DEFAULT,
            "script_timeout_max_seconds": SCRIPT_TIMEOUT_MAX,
            "start_permit_ttl_seconds": LIMITS["start_permit_ttl_seconds"],
            "effectful_per_project": LIMITS["effectful_per_project"],
            "readonly_parallel_per_project": LIMITS["readonly_parallel_per_project"],
            "pending_queue": LIMITS["pending_queue"],
            "offline_wait_seconds": LIMITS["offline_wait_seconds"],
        },
        # Declared but not enforced: named so an operator can see the difference,
        # never placed in ``limits`` where a client would read it as a cap.
        "advisory_limits": {
            key: LIMITS[key] for key in ADVISORY_LIMITS if key in LIMITS},
        "artifact_protocol": CAPABILITY["artifact_protocol"],
        "single_source": "contracts/desktop/v2.json",
    }


def validate_hello_execution(block: Any, *, platform: str = "posix") -> List[str]:
    """Validate the block a *device* reports in its ``hello`` (task 6.1).

    The device states what it can do; the server records it and uses it for the
    intersection with the user's scope. Two rules matter: the declared tool set
    may only *narrow* the contract's list (a device cannot invent a tool the
    server would then proxy), and every declared limit must be one the contract
    knows, so a client cannot smuggle a claim like "unlimited timeout" through
    the intersection.
    """
    problems: List[str] = []
    if not isinstance(block, dict):
        return ["hello project_execution block is not an object"]
    for key in ("protocol_major", "protocol_minor", "tools", "tool_schema_version",
                "limits"):
        if block.get(key) in (None, ""):
            problems.append("hello project_execution is missing %r" % key)
    if block.get("protocol_major") is not None \
            and block.get("protocol_major") != PROTOCOL_MAJOR:
        problems.append("hello project_execution.protocol_major must be %d"
                        % PROTOCOL_MAJOR)
    if block.get("tool_schema_version") is not None \
            and block.get("tool_schema_version") != TOOL_SCHEMA_VERSION:
        problems.append("hello project_execution.tool_schema_version must be %d"
                        % TOOL_SCHEMA_VERSION)
    tools = block.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            problems.append("hello project_execution.tools must be a list of strings")
        else:
            unknown = sorted(set(tools) - set(TOOLS["required"]))
            if unknown:
                problems.append("hello project_execution.tools names unknown tools: %s"
                                % ", ".join(unknown))
            unsupported = sorted(t for t in tools if not tool_supported_on(t, platform))
            if unsupported:
                problems.append("hello project_execution.tools claims %s on %s"
                                % (", ".join(unsupported), platform))
    limits = block.get("limits")
    if limits is not None:
        if not isinstance(limits, dict):
            problems.append("hello project_execution.limits must be an object")
        else:
            for key, value in limits.items():
                if key not in LIMITS:
                    problems.append("hello project_execution.limits names unknown limit %r"
                                    % key)
                elif not isinstance(value, int) or isinstance(value, bool):
                    problems.append("hello project_execution.limits.%s must be an integer"
                                    % key)
                elif value < 0:
                    problems.append("hello project_execution.limits.%s must not be negative"
                                    % key)
    problems.extend(validate_capability_block(
        dict(block, **_capability_defaults(block))))
    return problems


def _capability_defaults(block: Dict[str, Any]) -> Dict[str, Any]:
    """Fill the meta-only keys a hello block legitimately omits.

    A device does not report ``available``/``platform``/``artifact_protocol``:
    those are the server's composition. Validating the hello's *declaration*
    means adding them as ``False``/empty rather than pretending the device sent
    them, so one validator covers both shapes.
    """
    filled: Dict[str, Any] = {}
    filled.setdefault("available", block.get("available", False))
    filled.setdefault("reason", block.get("reason", "not_implemented"))
    for key in ("platform", "runtime", "artifact_protocol", "single_source",
                "files_write_verified", "scripts_verified", "required"):
        if key not in block:
            filled[key] = {
                "platform": "posix", "runtime": "", "artifact_protocol":
                    CAPABILITY["artifact_protocol"], "single_source": "contracts/desktop/v2.json",
                "files_write_verified": False, "scripts_verified": False,
                "required": False,
            }[key]
    return filled


def validate_capability_block(block: Any) -> List[str]:
    """Validate the meta ``project_execution`` block (no user data, no claims).

    Two things are checked beyond shape: an unavailable block states *why*, and
    the block never carries the forbidden keys -- the same rule v1's meta
    envelope has, because this block is served to an unauthenticated caller.
    """
    problems: List[str] = []
    if not isinstance(block, dict):
        return ["project_execution block is not an object"]
    for key in CAPABILITY["required"]:
        if key not in block:
            problems.append("project_execution is missing %r" % key)
    for key in CAPABILITY["forbidden"]:
        if key in block:
            problems.append("project_execution must not contain %r" % key)
    for key in ("available", "required", "files_write_verified", "scripts_verified"):
        if key in block and not isinstance(block[key], bool):
            problems.append("project_execution.%s must be a boolean" % key)
    if block.get("available") is False and not block.get("reason"):
        problems.append("an unavailable capability must state a reason")
    reason = block.get("reason")
    if reason is not None and reason not in CAPABILITY["reason_values"]:
        problems.append("unknown capability reason %r" % reason)
    if block.get("protocol_major") is not None \
            and block.get("protocol_major") != PROTOCOL_MAJOR:
        problems.append("project_execution.protocol_major must be %d" % PROTOCOL_MAJOR)
    if block.get("required") is True:
        problems.append("project_execution must stay optional")
    surfaces = block.get("surfaces")
    if surfaces is not None:
        if not isinstance(surfaces, dict):
            problems.append("project_execution.surfaces must be an object")
        else:
            unknown = sorted(set(surfaces) - set(CAPABILITY["surface_names"]))
            if unknown:
                problems.append("project_execution.surfaces names %s"
                                % ", ".join(unknown))
            for name, entry in surfaces.items():
                if not isinstance(entry, dict):
                    problems.append("project_execution.surfaces.%s must be an object" % name)
                    continue
                for key in CAPABILITY["surface_required"]:
                    if key not in entry:
                        problems.append("project_execution.surfaces.%s is missing %r"
                                        % (name, key))
                if entry.get("available") is not None \
                        and not isinstance(entry["available"], bool):
                    problems.append(
                        "project_execution.surfaces.%s.available must be a boolean" % name)
                if entry.get("available") is False and not entry.get("reason"):
                    problems.append(
                        "an unavailable surface must state a reason: %s" % name)
    tools = block.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            problems.append("project_execution.tools must be a list of strings")
        else:
            unknown = sorted(set(tools) - set(TOOLS["required"]))
            if unknown:
                problems.append("project_execution.tools names unknown tools: %s"
                                % ", ".join(unknown))
            if block.get("scripts_verified") and not set(TOOLS["script_tools"]) & set(tools):
                problems.append(
                    "scripts_verified cannot be true without a script tool in tools")
    limits = block.get("limits")
    if limits is not None and not isinstance(limits, dict):
        problems.append("project_execution.limits must be an object")
    if isinstance(limits, dict):
        for key, value in limits.items():
            if key not in LIMITS:
                problems.append("project_execution.limits names unknown limit %r" % key)
            elif not isinstance(value, int) or isinstance(value, bool):
                problems.append("project_execution.limits.%s must be an integer" % key)
            elif key in ADVISORY_LIMITS:
                # The whole point of splitting the two dicts: a client must never
                # find an unenforced budget in the one it reads as "the caps".
                problems.append(
                    "project_execution.limits.%s is advisory and not enforced" % key)
    advisory = block.get("advisory_limits")
    if advisory is not None and not isinstance(advisory, dict):
        problems.append("project_execution.advisory_limits must be an object")
    if isinstance(advisory, dict):
        for key, value in advisory.items():
            if key not in LIMITS:
                problems.append(
                    "project_execution.advisory_limits names unknown limit %r" % key)
            elif not isinstance(value, int) or isinstance(value, bool):
                problems.append(
                    "project_execution.advisory_limits.%s must be an integer" % key)
    if isinstance(advisory, dict) and isinstance(limits, dict):
        overlap = sorted(set(advisory) & set(limits))
        if overlap:
            problems.append("project_execution limit %s is both advertised and advisory"
                            % ", ".join(overlap))
    try:
        encoded = json.dumps(block, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        problems.append("project_execution is not JSON-serializable")
        return problems
    if len(encoded) > _v1.META_ENVELOPE["max_bytes"]:
        problems.append("project_execution exceeds the meta payload cap")
    return problems
