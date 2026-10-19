"""The v3 (project execution, Windows file access) desktop contract.

Change ``add-windows-local-project-access``, task 3.1. ``contracts/desktop/v3.json``
is the contract as data and this module is the only reader of it on the Python
side; the desktop client reads the same file through
``desktop/src/main/project-execution/generated-contract-v3.ts``, rendered by
``scripts/gen_desktop_execution_types.py``.

Why a third module rather than a version argument on the v2 one
---------------------------------------------------------------
v2 is frozen: ``contracts/desktop/v2.json`` keeps ``win32.supported = false``, its
tool refusal and its POSIX/Bash semantics exactly as they were, because a v2
client in the field must keep getting the same answers. That freeze is the whole
point of a new protocol major, so ``auth/desktop_contracts_v2.py`` is not edited
at all -- it stays the reader for major 2, and this module is the reader for
major 3.

What v3 *adds* on top of v2:

* ``platforms.win32.supported = true`` with a per-platform tool set, so the five
  file tools are admitted on Windows while no script tool is (the document names
  ``launcher_kinds.scripts = ""``);
* ``operations`` -- an explicit, grant-bound authorization set that is required on
  the frame and covered by the start permit and the params digest, so a file
  grant can never authorize a script;
* ``expected_version`` -- an opaque precondition on ``write``/``edit`` that only a
  prior read through the same authorised root can satisfy;
* an optional ``run_script`` tool with a declared runtime.

Blocks that are genuinely identical to v2 (artifact, journal, relative paths,
phases, effects, statuses) are delegated to the v2 module rather than restated,
so the two cannot drift. Where the *tool set* or the *frame shape* differs -- and
only there -- v3 has its own implementation.

Nothing here authorizes anything. These are shape checks; the command service
still resolves identity, tenant, binding, grant version, approval and quota on
every request.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from auth import desktop_contracts as _v1
from auth import desktop_contracts_v2 as _v2

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT_PATH = os.path.join(_REPO, "contracts", "desktop", "v3.json")

#: The major this module reads. Kept here as the single Python constant the
#: broker, dispatch and capability code compare against.
PROTOCOL_MAJOR = 3

#: The majors this build can still serve, newest first. A client that declares an
#: older major is served by that major's own rules, never by v3's.
SUPPORTED_MAJORS = (3, 2)


def load_contract(path: Optional[str] = None) -> Dict[str, Any]:
    """Read the v3 contract document. Raises if it is missing or malformed."""
    with open(path or CONTRACT_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


CONTRACT = load_contract()

PROTOCOL: Dict[str, Any] = CONTRACT["protocol"]
PLATFORMS: Dict[str, Any] = CONTRACT["platforms"]
TOOLS: Dict[str, Any] = CONTRACT["tools"]
FRAMES: Dict[str, Any] = CONTRACT["frames"]
OPERATIONS: Dict[str, Any] = CONTRACT["operations"]
VERSION_PRECONDITION: Dict[str, Any] = CONTRACT["version_precondition"]
DIGEST: Dict[str, Any] = CONTRACT["digest"]
CAPABILITY: Dict[str, Any] = CONTRACT["capability"]
BROKER: Dict[str, Any] = CONTRACT["broker"]
INHERITS: Dict[str, Any] = CONTRACT["inherits"]

PROTOCOL_NAME = PROTOCOL["name"]
TOOL_SCHEMA_VERSION = TOOLS["schema_version"]

#: Phases, effects, artifact, journal and start-permit shapes are v2's, unchanged
#: and named as such by ``inherits``. Re-exported so a caller has one import.
PHASES: Dict[str, Any] = _v2.PHASES
EFFECTS: Dict[str, Any] = _v2.EFFECTS
ARTIFACT: Dict[str, Any] = _v2.ARTIFACT
JOURNAL: Dict[str, Any] = _v2.JOURNAL
START_PERMIT: Dict[str, Any] = _v2.START_PERMIT

#: v3 adds no error code of its own: it reuses v2's set, which already merges
#: v1's. ``status_for`` therefore delegates, and a code cannot change status
#: between majors.
ERROR_CODES: Dict[str, int] = dict(_v2.ERROR_CODES)


def effective_limits() -> Dict[str, Any]:
    """v2's effective limits plus v3's additions (currently none).

    v3 declares no ``limits`` block: every bound it enforces is named in
    ``inherits.limits`` and read from v2, which already folded in v1's. So there
    is no third place a bound could be written.
    """
    return dict(_v2.LIMITS)


LIMITS: Dict[str, Any] = effective_limits()

#: Script runtimes the contract knows. A runtime that is not here can never be
#: requested, whatever a client claims.
RUNTIME_LANGUAGES: Tuple[str, ...] = tuple(
    TOOLS.get("arguments", {}).get("run_script", {}).get("runtimes") or ())

#: Tools that require more than the file grant -- an extra native confirmation.
TOOLS_REQUIRING_EXTRA_CONFIRMATION: Tuple[str, ...] = tuple(
    OPERATIONS.get("requires_extra_confirmation") or ())


def status_for(code: str) -> Optional[int]:
    """The HTTP status an error code must carry. Delegated to v2/v1."""
    return _v2.status_for(code)


def validate_error_status(code: str, status: int) -> List[str]:
    return _v2.validate_error_status(code, status)


# ---------------------------------------------------------------------------
# Platform and tool admission
# ---------------------------------------------------------------------------

def platform_supported(platform: str) -> bool:
    """Whether this contract admits a launcher for ``platform``.

    Both posix and win32 are admitted in v3. This is *not* a claim that Windows
    execution is verified: ``files_write_verified``/``scripts_verified`` and the
    accepted capability slice still have to say so, exactly as they do for posix.
    """
    if platform == "posix":
        # posix is the platform v2 already accepted; keep its answers stable.
        return _v2.platform_supported("posix")
    spec = PLATFORMS.get(platform)
    return bool(isinstance(spec, dict) and spec.get("supported") is True)


def launcher_kinds(platform: str) -> Dict[str, str]:
    """Which launcher kind is admitted per surface, or ``{}`` when unsupported."""
    if not platform_supported(platform):
        return {}
    spec = PLATFORMS.get(platform) or {}
    kinds = spec.get("launcher_kinds")
    if not isinstance(kinds, dict):
        # A platform with a single launcher serves both surfaces from it.
        launcher = spec.get("launcher") or ""
        return {"files": launcher, "scripts": launcher}
    return {name: str(value or "") for name, value in kinds.items()}


def tools_for_platform(platform: str) -> List[str]:
    """The tools this contract offers on ``platform``, in document order.

    An unadmitted platform offers nothing. On an admitted platform the per-platform
    set is authoritative: that is how the document says "Windows offers the file
    tools and no script tool" as data rather than as a runtime guess.
    """
    if not platform_supported(platform):
        return []
    declared = (TOOLS.get("by_platform") or {}).get(platform)
    if not isinstance(declared, list):
        # No per-platform set: every required tool except the script tools whose
        # launcher kind is empty for this platform.
        kinds = launcher_kinds(platform)
        if not kinds.get("scripts"):
            return [tool for tool in TOOLS["required"]
                    if tool not in TOOLS["script_tools"]]
        return list(TOOLS["required"])
    return [tool for tool in TOOLS["required"] if tool in declared]


def tool_supported_on(tool: str, platform: str) -> bool:
    """Whether this build's v3 client may run ``tool`` on ``platform``."""
    if tool not in TOOLS["required"]:
        return False
    return tool in tools_for_platform(platform)


def runtime_supported_on(language: str, platform: str) -> bool:
    """Whether ``language`` may be requested on ``platform``.

    A script tool needs both an admitted script launcher and a runtime the
    contract names. Today only posix names one.
    """
    if language not in RUNTIME_LANGUAGES:
        return False
    if not any(tool in TOOLS["script_tools"] for tool in tools_for_platform(platform)):
        return False
    return bool(launcher_kinds(platform).get("scripts"))


def operation_known(name: str) -> bool:
    return name in (OPERATIONS.get("names") or ())


def tool_requires_operation(tool: str) -> Optional[str]:
    """The operation a tool cannot run without, or None for an unknown tool.

    This is the mapping that makes ``operations`` worth having: the five file
    tools need ``files``, and ``run_script``/``bash`` need ``scripts``. So a
    project opened for editing is still not open for running code in it.
    """
    for name, members in (OPERATIONS.get("required_for") or {}).items():
        if tool in members:
            return name
    return None


def tools_under_operation(name: str) -> List[str]:
    """Every tool an operation opens, in document order.

    Read from ``required_for``, which is the authoritative statement of what each
    operation gates. ``sets`` is the same thing written as groups and is checked
    against ``required_for`` by ``contract_problems``; it is not a second source.
    """
    if not operation_known(name):
        return []
    members = (OPERATIONS.get("required_for") or {}).get(name) or ()
    return [tool for tool in TOOLS["required"] if tool in members]


# ---------------------------------------------------------------------------
# operations and the version precondition
# ---------------------------------------------------------------------------

def validate_operations(value: Any, *, tool: Optional[str] = None) -> List[str]:
    """Check an ``operations`` set is a real, non-empty authorization.

    The empty set is refused rather than treated as "no restriction": a frame
    that cannot name what it is spending must not reach a project directory.
    """
    problems: List[str] = []
    if value is None:
        return ["operations is required"]
    if not isinstance(value, (list, tuple)):
        return ["operations must be a list of operation names"]
    if not value:
        return ["operations must not be empty"]
    seen: List[str] = []
    for name in value:
        if not isinstance(name, str) or not name:
            problems.append("every operation name must be a non-empty string")
            continue
        if not operation_known(name):
            problems.append("unknown operation %r" % name)
            continue
        if name in seen:
            problems.append("operation %r is named twice" % name)
        seen.append(name)
    if problems:
        return problems
    if tool is not None:
        needed = tool_requires_operation(tool)
        if needed is None:
            problems.append("tool %r is opened by no operation" % tool)
        elif needed not in seen:
            problems.append("tool %r needs the %r operation" % (tool, needed))
    return problems


def authorized(spent: Any, granted: Any) -> bool:
    """Whether everything a frame spends was actually granted.

    The client's ``operations`` is a *claim*; the grant's set is the authority.
    A claim that is not a subset of the grant is refused, and so is an empty
    grant or an empty claim: a frame that names nothing it is spending has not
    been authorized for anything.
    """
    if not isinstance(spent, (list, tuple)) or not isinstance(granted, (list, tuple)):
        return False
    if not spent or not granted:
        return False
    return set(spent) <= set(granted)


def needs_extra_confirmation(tool: str) -> bool:
    """Whether ``tool`` needs a confirmation beyond the file grant."""
    return tool in TOOLS_REQUIRING_EXTRA_CONFIRMATION


def validate_expected_version(value: Any, *, tool: str,
                              requires: bool = True) -> List[str]:
    """The precondition that makes an overwrite safe.

    The value is opaque on purpose: it binds the project, the relative path, the
    object identity and the content, so a caller cannot satisfy it by hashing
    whatever it happens to hold. Only a prior read through the same authorised
    root produces one.

    ``requires`` is decided by the caller, because it depends on the arguments
    rather than the tool name alone: ``edit`` always needs a version, while
    ``write`` needs one for every overwrite and only an explicit
    ``create: true`` is exempt.
    """
    if value in (None, "") and requires:
        return ["%s requires expected_version" % tool]
    if value in (None, ""):
        return []
    if not isinstance(value, str):
        return ["expected_version must be a string"]
    if not _v2._looks_like_digest(value) and len(value) < 16:
        # The transport is a string; a value too short to bind anything is a bug
        # on the caller's side, not a version that simply does not match.
        return ["expected_version is too short to identify a version"]
    return []


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def contract_problems() -> List[str]:
    """Cross-document consistency, checked by tests rather than at import time."""
    problems: List[str] = []

    # -- every inherited key must exist below, and must not be restated here.
    inherited_limits = list(INHERITS.get("limits") or ())
    if "limits" in CONTRACT:
        problems.append("v3 declares no limits; inherited bounds must not be restated")
    for key in inherited_limits:
        if key not in _v2.LIMITS:
            problems.append("inherits.limits names %r, which v2 does not define" % key)
        if key not in LIMITS:
            problems.append("inherited limit %r is not in the effective set" % key)
    # Blocks v3 partially restates on top of v2's. ``frames`` and
    # ``start_permit`` are inherited field by field -- the fields v3 adds are the
    # whole reason for the new major -- while the rest are taken whole.
    partial_overrides = ("frames", "start_permit")
    for key, wanted in INHERITS.items():
        if key in ("note", "from", "limits"):
            continue
        if wanted is True and key in CONTRACT and key not in partial_overrides:
            problems.append("%r is inherited, so this document must not restate it"
                            % key)
    if INHERITS.get("from") != 2:
        problems.append("v3 inherits from v2")

    # -- the inherited blocks must really be v2's, not a private copy.
    for name, mine, theirs in (("phases", PHASES, _v2.PHASES),
                               ("effects", EFFECTS, _v2.EFFECTS),
                               ("artifact", ARTIFACT, _v2.ARTIFACT),
                               ("journal", JOURNAL, _v2.JOURNAL)):
        if mine != theirs:
            problems.append("inherited block %r differs from v2's" % name)
    if ERROR_CODES != _v2.ERROR_CODES:
        problems.append("v3 must not add or change error codes")

    # -- statuses, reuse the v2 rule.
    for code, status in ERROR_CODES.items():
        if not 400 <= status < 600:
            problems.append("error code %r has a non-error status %d" % (code, status))

    # -- tools.
    for tool in TOOLS["readonly"] + TOOLS["effectful"]:
        if tool not in TOOLS["required"]:
            problems.append("tool %r is not in tools.required" % tool)
    for tool in TOOLS["readonly"]:
        if tool in TOOLS["effectful"]:
            problems.append("tool %r is both readonly and effectful" % tool)
    for tool in TOOLS["script_tools"]:
        if tool not in TOOLS["effectful"]:
            problems.append("script tool %r must be effectful" % tool)
    for platform in ("posix", "win32"):
        if platform not in TOOLS.get("by_platform", {}):
            problems.append("tools.by_platform names no set for %r" % platform)
            continue
        for tool in TOOLS["by_platform"][platform]:
            if tool not in TOOLS["required"]:
                problems.append("platform %r offers unknown tool %r"
                                % (platform, tool))
        # A platform must not offer a script tool whose launcher kind is empty.
        kinds = launcher_kinds(platform)
        if not kinds.get("scripts"):
            for tool in TOOLS["by_platform"][platform]:
                if tool in TOOLS["script_tools"]:
                    problems.append(
                        "platform %r offers script tool %r with no script launcher"
                        % (platform, tool))

    # -- platforms.
    for name, spec in PLATFORMS.items():
        if name == "note":
            continue
        if not isinstance(spec, dict) or not isinstance(spec.get("supported"), bool):
            problems.append("platform %r has no supported flag" % name)
            continue
        if spec["supported"] and not spec.get("launcher"):
            problems.append("supported platform %r names no launcher" % name)
        if not spec["supported"] and spec.get("launcher"):
            problems.append("unsupported platform %r must not name a launcher" % name)
        kinds = spec.get("launcher_kinds")
        if isinstance(kinds, dict):
            if spec.get("launcher_kinds", {}).get("files") != spec.get("launcher"):
                problems.append(
                    "platform %r launcher_kinds.files disagrees with launcher" % name)
    if not platform_supported("posix"):
        problems.append("posix must stay an admitted platform")

    # -- operations.
    sets = OPERATIONS.get("sets") or {}
    for name, members in sets.items():
        if not isinstance(members, list) or not members:
            problems.append("operation set %r must be a non-empty list" % name)
            continue
        for tool in members:
            if tool not in TOOLS["required"]:
                problems.append("operation set %r names unknown tool %r"
                                % (name, tool))
    for name in OPERATIONS.get("names") or ():
        if name not in sets:
            problems.append("operation %r has no member set" % name)
    # The file operation must not open a script tool: that separation is the
    # reason operations exist at all. The two operations are otherwise
    # independent -- a script grant does not imply a file grant, and a caller
    # that needs both asks for both.
    file_tools = set(tools_under_operation("files"))
    script_tools = set(tools_under_operation("scripts"))
    if not file_tools:
        problems.append("the files operation opens no tool")
    if not script_tools:
        problems.append("the scripts operation opens no tool")
    for tool in TOOLS["script_tools"]:
        if tool in file_tools:
            problems.append("the files operation must not open script tool %r" % tool)
    # Every tool must name the operation it cannot run without, and that
    # operation must actually open it. ``required_for`` maps a tool to the
    # operation that gates it, so it is iterated as a tool -> operation map.
    required_for = OPERATIONS.get("required_for") or {}
    for tool in TOOLS["required"]:
        needed = tool_requires_operation(tool)
        if needed is None:
            problems.append("no operation names tool %r as required" % tool)
        elif needed not in (OPERATIONS.get("names") or ()):
            problems.append("tool %r requires unknown operation %r" % (tool, needed))
        elif tool not in (sets.get(needed) or ()):
            problems.append("operation %r does not open tool %r it is required for"
                            % (needed, tool))
    for operation, members in required_for.items():
        if operation not in (OPERATIONS.get("names") or ()):
            problems.append("required_for names unknown operation %r" % operation)
    if OPERATIONS.get("default") != []:
        problems.append("operations.default must be the empty set")
    for tool in OPERATIONS.get("requires_extra_confirmation") or ():
        if tool not in TOOLS["required"]:
            problems.append("requires_extra_confirmation names unknown tool %r" % tool)
    # The two operations must not be the same set, or "file access" and "run code"
    # would collapse back into the single permission v3 exists to split.
    if set(sets.get("files") or ()) == set(sets.get("scripts") or ()):
        problems.append("the files and scripts operations must not be the same set")    # Every digest-covered key must be a field the frame can actually carry.
    required = set(FRAMES["execute_tool"]["required"])
    optional = set(FRAMES["execute_tool"]["optional"])
    for key in DIGEST.get("fields") or ():
        if key not in required | optional:
            problems.append("digest covers %r, which no frame carries" % key)
    if not set(DIGEST.get("v2_fields") or ()) <= set(DIGEST.get("fields") or ()):
        problems.append("v3's digest must cover every field v2's did")

    # -- frames.
    spec = FRAMES["execute_tool"]
    if "operations" not in spec["required"]:
        problems.append("execute_tool must require operations")
    for key in spec["optional"]:
        if key in spec["required"]:
            problems.append("%r is both required and optional" % key)
    for key in spec["forbidden"]:
        if key in spec["required"]:
            problems.append("%r is required and forbidden at once" % key)

    # -- the version precondition must name a code the contract defines.
    if status_for(VERSION_PRECONDITION["stale_code"]) is None:
        problems.append("version_precondition.stale_code is not a known code")

    # -- broker.
    for key, path in BROKER["paths"].items():
        if not path.startswith("/api/desktop/execution/"):
            problems.append("broker path %r is outside /api/desktop/execution/" % key)
    if set(BROKER["methods"]) != set(BROKER["paths"]):
        problems.append("every broker path needs exactly one method")
    for key in ("operations", "runtime_verified"):
        if key not in BROKER["checks"]:
            problems.append("the broker must check %r" % key)
    return problems


def negotiate(server_protocols: Any) -> Tuple[bool, str]:
    """Whether a *v3* client may use the execution entry point against a server.

    A server that does not offer ``project_execution`` at major 3 makes the v3
    entry point unavailable. It is never downgraded into v2's frames or v1's
    read-only ops: a downgrade would drop ``operations`` and ``expected_version``,
    which are the two things that make a v3 write safe.
    """
    if not isinstance(server_protocols, dict):
        return False, "not_implemented"
    offered = server_protocols.get(PROTOCOL_NAME)
    if not isinstance(offered, dict):
        return False, "not_implemented"
    major = offered.get("major")
    if not isinstance(major, int) or isinstance(major, bool):
        return False, "protocol_incompatible"
    if major != PROTOCOL_MAJOR:
        return False, "protocol_incompatible"
    return True, "available"


def version_supported(major: Any) -> bool:
    """Whether this build can still serve a client declaring ``major``."""
    return isinstance(major, int) and not isinstance(major, bool) \
        and major in SUPPORTED_MAJORS


def advertised_major() -> int:
    """The major ``/api/desktop/meta`` may advertise in its ``protocols`` map.

    A public, device-less endpoint can carry exactly one version of one protocol
    name, so it cannot satisfy both majors at once: bumping it to 3 makes every
    v2 client read ``major 3 != 2`` and refuse the whole entry point, while
    leaving it at 2 makes a v3 client refuse. The resolution is that meta stays on
    the *oldest major this build must keep serving* and v3 is negotiated per
    device through ``hello``, whose block is per-lease and therefore can differ
    between a v2 and a v3 device on the same server.

    Returned rather than inlined so the handler and the tests read the same
    number, and so lowering :data:`SUPPORTED_MAJORS` (dropping v2 support) is what
    would finally let meta move to v3.
    """
    return min(SUPPORTED_MAJORS)


def validate_protocol_document(protocols: Any) -> List[str]:
    """Validate a meta/hello ``protocols`` map that claims to include v3."""
    problems: List[str] = []
    if not isinstance(protocols, dict):
        return ["protocols is not an object"]
    spec = protocols.get(PROTOCOL_NAME)
    if spec is None:
        return problems
    if not isinstance(spec, dict):
        return ["protocol %r has no version object" % PROTOCOL_NAME]
    if not isinstance(spec.get("major"), int) or isinstance(spec.get("major"), bool):
        problems.append("protocol %r has no integer major" % PROTOCOL_NAME)
    elif spec.get("major") != PROTOCOL_MAJOR:
        problems.append("protocol %r major must be %d" % (PROTOCOL_NAME, PROTOCOL_MAJOR))
    if spec.get("required") is not False:
        problems.append(
            "protocol %r must stay optional so a v2 peer keeps working"
            % PROTOCOL_NAME)
    return problems


# ---------------------------------------------------------------------------
# execute_tool
# ---------------------------------------------------------------------------

def validate_execute_frame(frame: Any, *, platform: str = "posix") -> List[str]:
    """Validate a v3 ``execute_tool`` frame.

    v3's own implementation rather than a call into v2's, because the things that
    differ are exactly the things this frame check exists to enforce: the tool
    allow-list contains ``run_script``, Windows admits the file tools, and
    ``operations`` is a required field instead of an unknown one.
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

    if not _v2._positive_int(frame.get("grant_version")):
        problems.append("grant_version must be a positive integer")
    if frame.get("selection_generation") is not None \
            and not _v2._non_negative_int(frame.get("selection_generation")):
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

    # The authorization the frame is spending, and whether this tool could ever
    # be covered by it.
    problems.extend(validate_operations(frame.get("operations"), tool=tool))

    # The runtime a script tool asks for must be one this contract knows, and one
    # this platform actually admits. ``run_script`` names its language; ``bash``
    # is POSIX shell and carries no language field, so it only needs the script
    # launcher, which the platform check above already enforced.
    runtime = frame.get("runtime")
    if isinstance(tool, str) and tool == "run_script":
        language = runtime
        if not language:
            arguments = frame.get("arguments")
            if isinstance(arguments, dict):
                language = arguments.get("language")
        if not language:
            problems.append("%s must name its language" % tool)
        elif not runtime_supported_on(str(language), platform):
            problems.append("runtime %r is unsupported on platform %r"
                            % (language, platform))
    elif runtime not in (None, ""):
        if str(runtime) not in RUNTIME_LANGUAGES:
            problems.append("unknown runtime %r" % runtime)

    arguments = frame.get("arguments")
    if arguments is not None:
        if not isinstance(arguments, dict):
            problems.append("arguments must be an object")
        else:
            for key in spec["forbidden"]:
                if key in arguments:
                    problems.append("execute_tool.arguments must not carry %r" % key)
            problems.extend(validate_arguments(tool, arguments))

    # expected_version may ride on the frame or in arguments; the contract allows
    # either, so it is read from both and the check runs even when the frame
    # carries no arguments at all (a write whose payload is a frame field).
    # ``edit`` always needs a version; ``write`` needs one for every overwrite and
    # only an explicit create is exempt.
    if isinstance(tool, str) and tool in ("write", "edit"):
        carried = frame.get("expected_version")
        if carried is None and isinstance(arguments, dict):
            carried = arguments.get("expected_version")
        creates = isinstance(arguments, dict) and arguments.get("create") is True
        requires = tool == "edit" or not creates
        problems.extend(validate_expected_version(carried, tool=tool,
                                                  requires=requires))
    digest = frame.get("params_digest")
    if digest is not None and not _v2._looks_like_digest(digest):
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


def validate_arguments(tool: str, arguments: Dict[str, Any]) -> List[str]:
    """Check a tool's arguments against the contract's declared shape.

    v3's own, because v3 is the version that documents ``write``/``edit``/
    ``run_script`` argument shapes at all. A tool the contract does not describe
    here is passed to the v2 argument rules, which is what keeps the five file
    tools' existing checks in one place.
    """
    spec = (TOOLS.get("arguments") or {}).get(tool)
    if not isinstance(spec, dict):
        return _v2._validate_arguments(tool, arguments)
    problems: List[str] = []
    for key in spec.get("forbidden") or ():
        if key in arguments:
            problems.append("%s.arguments must not carry %r" % (tool, key))
    for key in spec.get("required") or ():
        if arguments.get(key) in (None, ""):
            problems.append("%s.arguments is missing %r" % (tool, key))
    if tool == "run_script":
        language = arguments.get("language")
        if language not in RUNTIME_LANGUAGES:
            problems.append("run_script.language must be one of %s"
                            % ", ".join(RUNTIME_LANGUAGES))
        script = arguments.get("script")
        if not isinstance(script, str) or not script.strip():
            problems.append("run_script.script must be a non-empty string")
        timeout = arguments.get("timeout")
        if timeout is not None:
            if not _v2._positive_int(timeout):
                problems.append("run_script.timeout must be a positive integer")
            elif timeout > LIMITS["script_timeout_max_seconds"]:
                problems.append("run_script.timeout must be <= %d seconds"
                                % LIMITS["script_timeout_max_seconds"])
    return problems


def validate_start_permit(permit: Any) -> List[str]:
    """v2's permit shape plus the operations the permit was issued for."""
    problems = _v2.validate_start_permit(permit)
    if not isinstance(permit, dict):
        return problems
    problems.extend(validate_operations(permit.get("operations")))
    return problems


# ---------------------------------------------------------------------------
# Capability block
# ---------------------------------------------------------------------------

def execution_capability(*, platform: str = "posix",
                         tools: Optional[List[str]] = None,
                         available: bool = False,
                         reason: str = "not_implemented",
                         files_write_verified: bool = False,
                         scripts_verified: bool = False,
                         runtime: str = "") -> Dict[str, Any]:
    """The optional ``project_execution`` block for meta/hello, at major 3.

    Built here rather than delegated to v2's, because v2's block answers for
    major 2 and for v2's tool set: it would report ``protocol_major: 2`` and
    refuse ``run_script`` before this contract's own rules were consulted. The
    shape stays v2's, with ``launcher_kinds`` added so a Windows client can tell
    "file tools available, scripts not verified" from "platform unsupported"
    without inventing a platform flag.
    """
    supported = [tool for tool in TOOLS["required"] if tool_supported_on(tool, platform)]
    if tools is not None:
        supported = [tool for tool in supported if tool in tools]
    if available and not supported:
        # Nothing runnable here: report the platform, not a capability the launch
        # path would refuse a moment later.
        available, reason = False, "platform_unsupported"
    if available:
        reason = "available"
    script_tools = [tool for tool in supported if tool in TOOLS["script_tools"]]
    scripts = bool(scripts_verified and script_tools)
    advertised = {
        "script_timeout_default_seconds": LIMITS["script_timeout_default_seconds"],
        "script_timeout_max_seconds": LIMITS["script_timeout_max_seconds"],
        "start_permit_ttl_seconds": LIMITS["start_permit_ttl_seconds"],
        "effectful_per_project": LIMITS["effectful_per_project"],
        "readonly_parallel_per_project": LIMITS["readonly_parallel_per_project"],
        "pending_queue": LIMITS["pending_queue"],
        "offline_wait_seconds": LIMITS["offline_wait_seconds"],
    }
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
        "scripts_verified": scripts,
        "limits": advertised,
        # Declared but not enforced: named so an operator can see the difference,
        # never placed in ``limits`` where a client would read it as a cap.
        "advisory_limits": _v2.ADVISORY_LIMITS and {
            key: LIMITS[key] for key in _v2.ADVISORY_LIMITS if key in LIMITS},
        "artifact_protocol": CAPABILITY["artifact_protocol"],
        "single_source": "contracts/desktop/v3.json",
        # Which launcher kind is actually admitted per surface, so the Windows
        # "files yes, scripts not yet" state is data rather than a platform flag.
        "launcher_kinds": launcher_kinds(platform),
        "surfaces": {
            "files": {"available": bool(available), "reason": reason},
            "scripts": {"available": scripts,
                        "reason": reason if scripts else _scripts_reason(available, platform)},
        },
    }


def _scripts_reason(files_available: bool, platform: str) -> str:
    """Why the script surface is closed, using the most specific reason."""
    if not files_available:
        return "platform_unsupported" if not platform_supported(platform) else "not_accepted"
    if not launcher_kinds(platform).get("scripts"):
        return "platform_unsupported"
    return "runtime_unavailable"


def validate_capability_block(block: Any) -> List[str]:
    """v3's capability block: v2's required keys plus ``launcher_kinds``.

    Written here rather than delegated, because v2's version checks
    ``protocol_major`` against 2 and would fail a correct v3 block.
    """
    problems: List[str] = []
    if not isinstance(block, dict):
        return ["capability block is not an object"]
    for key in CAPABILITY["required"]:
        if key not in block:
            problems.append("capability block is missing %r" % key)
    if block.get("protocol_major") != PROTOCOL_MAJOR:
        problems.append("capability block protocol_major must be %d" % PROTOCOL_MAJOR)
    if block.get("required") is not False:
        problems.append("capability block.required must stay false")
    if block.get("reason") not in (CAPABILITY.get("reason_values") or ()):
        problems.append("capability block reason %r is not a known reason"
                        % block.get("reason"))
    if block.get("available") is True and block.get("reason") != "available":
        problems.append("an available block must report reason 'available'")
    if block.get("available") is False and block.get("reason") == "available":
        problems.append("an unavailable block must say why")
    tools = block.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            problems.append("capability block tools must be a list of strings")
        else:
            unknown = sorted(set(tools) - set(TOOLS["required"]))
            if unknown:
                problems.append("capability block names unknown tools: %s"
                                % ", ".join(unknown))
    for key in CAPABILITY["forbidden"]:
        if key in block:
            problems.append("capability block must not carry %r" % key)
    for key in ("available", "required", "files_write_verified", "scripts_verified"):
        if key in block and not isinstance(block[key], bool):
            problems.append("capability block.%s must be a boolean" % key)
    # The two surfaces must agree with the flags beside them.
    if block.get("scripts_verified") and not any(
            tool in TOOLS["script_tools"] for tool in (tools or ())):
        problems.append(
            "scripts_verified cannot be true without a script tool in tools")
    kinds = block.get("launcher_kinds")
    if kinds is not None and not isinstance(kinds, dict):
        problems.append("launcher_kinds must be an object")
    surfaces = block.get("surfaces")
    if surfaces is not None:
        if not isinstance(surfaces, dict):
            problems.append("surfaces must be an object")
        else:
            for name in CAPABILITY.get("surface_names") or ():
                surface = surfaces.get(name)
                if surface is None:
                    continue
                if not isinstance(surface, dict):
                    problems.append("surface %r must be an object" % name)
                    continue
                for key in CAPABILITY["surface_required"]:
                    if key not in surface:
                        problems.append("surface %r is missing %r" % (name, key))
    return problems


def validate_hello_execution(block: Any, *, platform: str = "posix") -> List[str]:
    """A device's declared v3 capability, narrowed to what the contract offers."""
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
    return problems


# ---------------------------------------------------------------------------
# Delegated, provably identical to v2
# ---------------------------------------------------------------------------
# These blocks are named in ``inherits`` as v2's, so re-exporting v2's
# implementation is the only way they cannot drift. They are bound here rather
# than re-implemented so a caller imports one module.

validate_result_frame = _v2.validate_result_frame
validate_heartbeat_frame = _v2.validate_heartbeat_frame
validate_artifact = _v2.validate_artifact
validate_journal_entry = _v2.validate_journal_entry
validate_relative_path = _v2.validate_relative_path
phase_for = _v2.phase_for
effects_for = _v2.effects_for
is_terminal_phase = _v2.is_terminal_phase
next_command_state = _v2.next_command_state
dedup_conflict = _v2.dedup_conflict
