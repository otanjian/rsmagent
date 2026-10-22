#!/usr/bin/env python
# encoding:utf-8
"""Generate the desktop client's contract constants from the contract documents.

Change ``align-desktop-project-execution-with-master``, task 6.1; extended for a
second protocol major by change ``add-windows-local-project-access``, task 3.1.

``contracts/desktop/v2.json`` and ``contracts/desktop/v3.json`` are the single
sources; this script turns each into a generated TypeScript module (``generated-contract.ts``
for v2, ``generated-contract-v3.ts`` for v3) so the client does not keep a second
hand-written copy of the phase table, the tool list or the limits. The committed
files are re-rendered by ``tests/test_desktop_execution_types.py`` (v2) and its v3
counterpart (v3), which fail when a file is stale -- that is what makes
"generated" a fact rather than a comment.

The default version is 2, and ``CONTRACT``/``OUTPUT``/``build()``/``main([])``
keep meaning v2, so the existing v2 tests address the same artifacts they always
did. A protocol major is a separate wire format, so each version gets its own
generated module rather than one file with a version switch inside it.

Usage:
    .venv/bin/python scripts/gen_desktop_execution_types.py          # write both
    .venv/bin/python scripts/gen_desktop_execution_types.py --check  # verify both
    .venv/bin/python scripts/gen_desktop_execution_types.py --version 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The protocol majors this generator knows, lowest first. Each entry maps a
#: major to its contract document and the module it renders; a major that
#: inherits keys names the major below it in its own document's ``inherits``.
PROTOCOL_MAJORS = (2, 3)

#: The major ``CONTRACT``/``OUTPUT``/``build()`` address when no version is
#: given, so the existing v2 tests keep addressing the same artifact.
DEFAULT_MAJOR = 2


def contract_path(major: int = DEFAULT_MAJOR) -> str:
    return os.path.join(REPO, "contracts", "desktop", "v%d.json" % major)


def output_path(major: int = DEFAULT_MAJOR) -> str:
    name = "generated-contract.ts" if major == DEFAULT_MAJOR \
        else "generated-contract-v%d.ts" % major
    return os.path.join(REPO, "desktop", "src", "main", "project-execution", name)


CONTRACT = contract_path(DEFAULT_MAJOR)
OUTPUT = output_path(DEFAULT_MAJOR)


def header_for(major: int) -> str:
    """The banner a generated module carries, naming its own source document."""
    rel = "contracts/desktop/v%d.json" % major
    return """/**
 * GENERATED FILE -- do not edit.
 *
 * Source: %s (project-execution protocol, major %d).
 * Regenerate: .venv/bin/python scripts/gen_desktop_execution_types.py
 * Checked by: tests/test_desktop_execution_types.py (fails when stale)
 *
 * Behaviour lives in ./contract.ts; only values and types are generated here,
 * so a phase, a tool or a limit changes in exactly one place.
 */

""" % (rel, major)


#: v2's banner, kept under its original name. The v2 tests assert on this text.
HEADER = header_for(2)

#: The platform table of the document currently being rendered. ``render`` sets
#: it before emitting tools, so ``platform_tools`` can tell an admitted platform
#: from one the document refuses.
_PLATFORMS: Dict[str, Any] = {}


def _ts_string_list(values: List[str]) -> str:
    return "[" + ", ".join("'%s'" % value for value in values) + "]"


def _ts_literal_list(values: List[str]) -> str:
    body = ",\n".join("    '%s'" % value for value in values)
    return "[\n%s,\n] as const" % body


def _ts_record(mapping: Dict[str, str]) -> str:
    body = "\n".join("    %s: '%s'," % (_key(key), value)
                     for key, value in sorted(mapping.items()))
    return "{\n%s\n}" % body


def _key(name: str) -> str:
    """A TS object key: bare when it is a plain identifier, quoted otherwise."""
    if name.isidentifier():
        return name
    return "'%s'" % name


def render(contract: Dict[str, Any], major: int = DEFAULT_MAJOR) -> str:
    """Render one protocol major's constants.

    ``contract`` is the document as authored -- needed because a missing
    ``limits`` block is what says "this major declares none of its own". The
    blocks it inherits are materialised here, so the renderer below only ever
    reads a complete document; ``inherits`` is therefore the only place that
    decides which version a value lives in.
    """
    global _PLATFORMS
    resolved = resolved_contract(major)
    protocol = resolved["protocol"]
    tools = resolved["tools"]
    frames = resolved["frames"]
    phases = resolved["phases"]
    effects = resolved["effects"]
    platforms = resolved["platforms"]
    capability = resolved["capability"]
    limits = effective_limit_values(major)
    _PLATFORMS = platforms
    text = [header_for(major)]

    text.append("/** The v2 protocol this client speaks. Optional: v1-only peers keep working. */\n")
    text.append("export const PROJECT_EXECUTION_PROTOCOL = {\n"
                "    major: %d,\n"
                "    minor: %d,\n"
                "    required: %s,\n"
                "} as const\n\n" % (protocol["major"], protocol["minor"],
                                    "true" if protocol["required"] else "false"))
    text.append("export const PROJECT_EXECUTION_NAME = '%s'\n\n" % protocol["name"])

    text.append("/** Frame types v2 adds on top of v1's gateway frames. */\n")
    text.append("export const EXECUTION_FRAME_TYPES = %s\n\n"
                % _ts_literal_list(frames["types"]))
    text.append("/** Fields an execute_tool frame must carry. */\n")
    text.append("export const EXECUTE_TOOL_REQUIRED = %s\n\n"
                % _ts_literal_list(frames["execute_tool"]["required"]))
    text.append("/** Fields an execute_tool frame may carry. */\n")
    text.append("export const EXECUTE_TOOL_OPTIONAL = %s\n\n"
                % _ts_literal_list(frames["execute_tool"]["optional"]))
    text.append("/** Fields an execute_tool frame must never carry. */\n")
    text.append("export const FORBIDDEN_FRAME_FIELDS = %s\n\n"
                % _ts_literal_list(frames["execute_tool"]["forbidden"]))

    text.append("/** The master tool names a v2 frame may carry. */\n")
    text.append("export const EXECUTION_TOOLS = %s\n\n" % _ts_literal_list(tools["required"]))
    text.append("export const EFFECTFUL_TOOLS = %s\n\n" % _ts_literal_list(tools["effectful"]))
    text.append("export const READONLY_TOOLS = %s\n\n" % _ts_literal_list(tools["readonly"]))
    text.append("export const SCRIPT_TOOLS = %s\n\n" % _ts_literal_list(tools["script_tools"]))
    text.append("/** The frozen argument shape these tools belong to. */\n")
    text.append("export const TOOL_SCHEMA_VERSION = %d\n\n" % tools["schema_version"])

    text.append("/** The visible phases -- projected, never stored as a state. */\n")
    text.append("export const EXECUTION_PHASES = %s\n\n" % _ts_literal_list(phases["names"]))
    text.append("export type ExecutionPhase = (typeof EXECUTION_PHASES)[number]\n\n")
    text.append("/** Terminal phases: the result has arrived and will not change. */\n")
    text.append("export const TERMINAL_PHASES: readonly ExecutionPhase[] = %s\n\n"
                % _ts_string_list(phases["terminal"]))
    text.append("/** The server command states this client may see, mapped to their phase. */\n")
    text.append("export const STATE_TO_PHASE: Readonly<Record<string, ExecutionPhase>> = %s\n\n"
                % _ts_record(phases["from_state"]))
    text.append("/** Phases a cancellation request changes the visible phase of. */\n")
    text.append("export const CANCELLING_OVERRIDES: readonly ExecutionPhase[] = %s\n\n"
                % _ts_string_list(phases["cancelling_overrides"]))
    text.append("/** The error code that means 'may have written, no conclusion'. */\n")
    text.append("export const OUTCOME_UNKNOWN_CODE = '%s'\n\n"
                % phases["outcome_unknown_code"])

    text.append("/** What a terminal result may claim about the project directory. */\n")
    text.append("export const EXECUTION_EFFECTS = %s\n\n" % _ts_literal_list(effects["names"]))
    text.append("export type ExecutionEffects = (typeof EXECUTION_EFFECTS)[number]\n\n")
    text.append("/** The default effect per phase (before any error code narrows it). */\n")
    text.append("export const EFFECTS_BY_PHASE: Readonly<Record<string, ExecutionEffects>> = %s\n\n"
                % _ts_record(effects["by_phase"]))
    text.append("/** The effect a specific error code proves. */\n")
    text.append("export const EFFECTS_BY_ERROR_CODE: Readonly<Record<string, ExecutionEffects>> = %s\n\n"
                % _ts_record(effects["by_error_code"]))

    # v2's own wording here is asserted by the v2 test that re-renders this file
    # byte for byte, so it is chosen from the major rather than reworded.
    limits_banner = ("/** Bounds this client enforces locally, using the v1 values v2 "
                     "reuses. */" if major == 2 else
                     "/** Bounds this client enforces locally, using the values it "
                     "inherits. */")
    text.append(limits_banner + "\n")
    text.append("export const EXECUTION_LIMITS = {\n")
    for key in sorted(_limit_keys(contract, major)):
        text.append("    %s: %s,\n" % (key, _limit_value(limits, key)))
    text.append("} as const\n\n")

    text.append("/** Whether the contract declares an accepted launcher per platform. */\n")
    text.append("export const PLATFORM_SUPPORT: Readonly<Record<string, boolean>> = {\n")
    for name in sorted(k for k in platforms if k != "note"):
        text.append("    %s: %s,\n" % (_key(name),
                                       "true" if platforms[name]["supported"] else "false"))
    text.append("}\n\n")
    text.append("/** Tools each supported platform offers (script tools excluded). */\n")
    text.append("export const PLATFORM_TOOLS: Readonly<Record<string, string[]>> = {\n")
    for name in sorted(k for k in platforms if k != "note"):
        text.append("    %s: %s,\n" % (_key(name), _ts_string_list(platform_tools(tools, name))))
    text.append("}\n\n")

    text.append("/** Permits are single use and short lived. */\n")
    text.append("export const START_PERMIT_TTL_SECONDS = %s\n"
                % _limit_value(limits, "start_permit_ttl_seconds"))
    text.append("export const START_PERMIT_SINGLE_USE = %s\n\n"
                % ("true" if limits["start_permit_single_use"] else "false"))

    # Blocks a later major adds. Emitted only when the document defines them, so
    # an earlier major's generated module is byte-for-byte what it always was.
    if "operations" in contract:
        operations = contract["operations"]
        text.append("/** The operations a frame may spend. File access and code "
                    "execution are different ones. */\n")
        text.append("export const OPERATION_NAMES = %s\n\n"
                    % _ts_literal_list(operations["names"]))
        text.append("/** Every tool each operation opens. */\n")
        text.append("export const OPERATION_TOOLS: Readonly<Record<string, string[]>> = {\n")
        for name in sorted(operations.get("required_for") or {}):
            text.append("    %s: %s,\n" % (_key(name),
                                           _ts_string_list(operations["required_for"][name])))
        text.append("}\n\n")
        text.append("/** The operation a tool cannot run without. */\n")
        text.append("export const TOOL_REQUIRED_OPERATION: Readonly<Record<string, string>> = {\n")
        for name in sorted(operations.get("required_for") or {}):
            for tool in operations["required_for"][name]:
                text.append("    %s: '%s',\n" % (_key(tool), name))
        text.append("}\n\n")
        text.append("/** An absent or old grant carries no operation at all. */\n")
        text.append("export const OPERATIONS_DEFAULT: readonly string[] = %s\n\n"
                    % _ts_string_list(operations.get("default") or []))
        text.append("/** Tools that need a confirmation beyond the file grant. */\n")
        text.append("export const TOOLS_REQUIRING_EXTRA_CONFIRMATION = %s\n\n"
                    % _ts_literal_list(operations.get("requires_extra_confirmation") or []))
        text.append("/** The refusal code each authorization failure reports. */\n")
        text.append("export const OPERATION_REFUSAL_CODES = %s\n\n"
                    % _ts_record(operations.get("refusal_codes") or {}))

    if "version_precondition" in contract:
        precondition = contract["version_precondition"]
        text.append("/** The precondition that makes an overwrite safe. */\n")
        text.append("export const EXPECTED_VERSION_FIELD = '%s'\n"
                    % precondition["field"])
        text.append("export const EXPECTED_VERSION_STALE_CODE = '%s'\n"
                    % precondition["stale_code"])
        text.append("export const EXPECTED_VERSION_REQUIRED_BY = %s\n"
                    % _ts_literal_list(precondition.get("required_by") or []))
        text.append("export const EXPECTED_VERSION_RETURNED_BY = %s\n\n"
                    % _ts_literal_list(precondition.get("returned_by") or []))

    if "digest" in contract:
        digest = contract["digest"]
        text.append("/** The fields the params digest covers, in this major. */\n")
        text.append("export const DIGEST_FIELDS_V3 = %s\n\n"
                    % _ts_literal_list(digest["fields"]))
        text.append("/** The fields the previous major covered, for a downgrade check. */\n")
        text.append("export const DIGEST_FIELDS_PREVIOUS = %s\n\n"
                    % _ts_literal_list(digest.get("v2_fields") or []))

    text.append("/** The artifact protocol this client produces references for. */\n")
    text.append("export const ARTIFACT_PROTOCOL = '%s'\n\n" % capability["artifact_protocol"])
    text.append("/** The meta/hello key this capability is reported under. */\n")
    text.append("export const EXECUTION_CAPABILITY_KEY = '%s'\n\n"
                % contract["meta_additions"]["keys"][0])
    text.append("/** Reasons a capability may report instead of 'available'. */\n")
    text.append("export const CAPABILITY_REASONS = %s\n\n"
                % _ts_literal_list(capability["reason_values"]))

    journal = resolved["journal"]
    text.append("/** Fields a local start-journal record must carry (execution-contract §4). */\n")
    text.append("export const JOURNAL_REQUIRED = %s\n\n"
                % _ts_literal_list(journal["required"]))
    text.append("/** Fields it may carry. */\n")
    text.append("export const JOURNAL_OPTIONAL = %s\n\n"
                % _ts_literal_list(journal["optional"]))
    text.append("/** The identity a redelivery is deduplicated against. */\n")
    text.append("export const JOURNAL_DEDUP_KEY = %s\n\n"
                % _ts_literal_list(journal["dedup_key"]))
    text.append("/** The code a same-id/different-payload redelivery is refused with. */\n")
    text.append("export const JOURNAL_CONFLICT_CODE = '%s'\n\n"
                % journal["conflict_code"])
    text.append("""/** One local start-journal record. Written before the worker can touch the project. */
export interface JournalRecord {
    journal_id: string
    command_id: string
    params_digest: string
    origin: string
    user_id: string
    tenant_id: string
    device_id: string
    workspace_id: string
    binding_id?: string
    run_id: string
    tool_call_id: string
    tool: string
    grant_version: number
    started_at: number
    connection_epoch?: string
    permit_id?: string
    skill_digests?: string[]
}

""")

    text.append("""export interface ExecutionCapability {
    available: boolean
    reason: string
    protocol_major: number
    protocol_minor: number
    required: boolean
    tools: string[]
    tool_schema_version: number
    platform: string
    runtime: string
    files_write_verified: boolean
    scripts_verified: boolean
    limits: Record<string, number>
    artifact_protocol?: string
    surfaces?: Record<string, { available: boolean; reason: string }>
}

/** A local artifact reference a result may carry. */
export interface ExecutionArtifact {
    source: string
    artifact_id: string
    device_id: string
    workspace_id: string
    run_id: string
    tool_call_id: string
    relative_path: string
    file_name: string
    kind: string
    size: number
    source_version: string
}

/** The result frame the execution end sends back. */
export interface ExecutionResultFrame {
    type: 'execution_result'
    protocol_major: number
    command_id: string
    run_id: string
    tool_call_id: string
    state: string
    execution_phase: ExecutionPhase
    effects: ExecutionEffects
    started_at: number
    finished_at: number
    exit_code?: number
    stdout?: string
    stderr?: string
    truncated?: boolean
    artifacts?: ExecutionArtifact[]
    error_code?: string | null
    error_message?: string | null
    heartbeat_at?: number
    cancel_requested?: boolean
    /**
     * The tool's own payload, as this end's tool produced it. A file tool's
     * answer travels here; stdout/stderr/exit_code describe a script.
     */
    result?: {
        status?: string
        result?: unknown
        display?: unknown
        ext_data?: unknown
        duration_ms?: number
    }
}
""")
    return "".join(text)


def resolved_contract(major: int = DEFAULT_MAJOR) -> Dict[str, Any]:
    """A contract with everything it inherits materialised, as plain data.

    v2 names the *limits* it takes from v1; v3 names whole blocks (phases,
    effects, artifact, journal, start_permit, frames, error codes) that it takes
    from v2 unchanged. ``inherits`` is the document saying "this key lives one
    version below"; this function walks that chain so the renderer only ever sees
    a complete document, and a key can still only be written in one place.
    """
    with open(contract_path(1), "r", encoding="utf-8") as handle:
        resolved: Dict[str, Any] = json.load(handle)
    for level in range(2, major + 1):
        with open(contract_path(level), "r", encoding="utf-8") as handle:
            document = json.load(handle)
        spec = document.get("inherits") or {}
        local = {key: value for key, value in document.items() if key != "inherits"}
        for key, wanted in spec.items():
            if key in ("note", "from"):
                continue
            if key == "limits":
                # Handled by effective_limit_values: limits merge key by key
                # across the whole chain rather than being taken as a block.
                continue
            if wanted is True and key in resolved:
                # Deep merge: a block the document partially restates keeps the
                # inherited entries it does not mention, and a key written
                # locally always wins. That is what lets v3 replace
                # frames.execute_tool's field list while keeping frames.types.
                local[key] = _deep_merge(resolved[key], local.get(key))
        for key, value in local.items():
            resolved[key] = value
    return resolved


def _deep_merge(base: Any, override: Any) -> Any:
    """Merge ``override`` onto ``base``; a locally written key always wins."""
    if not isinstance(base, dict) or not isinstance(override, dict):
        return base if override is None else override
    merged = dict(base)
    for key, value in override.items():
        merged[key] = _deep_merge(base[key], value) if key in base else value
    return merged


def _limit_keys(contract: Dict[str, Any], major: int = DEFAULT_MAJOR) -> List[str]:
    """Every limit the client needs, including the ones inherited from below.

    v2 names the keys it takes from v1 in ``limits.reuse_v1``; v3 has no
    ``limits`` block at all and names v2's effective set in ``inherits.limits``.
    Either way the value lives in exactly one document and this function never
    invents a third place for it. ``contract`` is the *raw* document, which is
    what tells the two cases apart.
    """
    if "limits" not in contract:
        inherited = list((contract.get("inherits") or {}).get("limits", ()))
        return sorted(key for key in inherited if key not in _LIMIT_DOC_KEYS)
    declared = [key for key in contract["limits"] if key not in _LIMIT_DOC_KEYS]
    return sorted(set(declared) | set(contract["limits"]["reuse_v1"]))


#: Keys in ``limits`` that are documentation or a classification, not a bound.
_LIMIT_DOC_KEYS = ("note", "reuse_v1", "advisory", "advisory_note")


def _limit_value(limits: Dict[str, Any], key: str) -> str:
    """Render one bound. ``limits`` is already the effective merged mapping."""
    raw = limits[key]
    if isinstance(raw, bool):
        return "true" if raw else "false"
    return str(raw)


def platform_tools(tools: Dict[str, Any], name: str) -> List[str]:
    """The tools one platform offers, as the contract document defines them.

    A contract that names its per-platform sets wins: that is how v3 says "Windows
    offers the five file tools and no script tool" as data rather than as a
    hardcoded platform check here. A contract without the block (v2) keeps the
    original rule -- every required tool except the script tools on win32.
    """
    by_platform = tools.get("by_platform")
    if isinstance(by_platform, dict) and name in by_platform:
        declared = by_platform[name]
        if not isinstance(declared, list):
            return []
        # Only tools the contract actually knows, in the document's own order.
        return [tool for tool in tools["required"] if tool in declared]
    # No declared set for this platform: offer nothing at all when the document
    # does not admit a launcher for it, and otherwise every required tool except
    # the script tools (v2's rule -- Windows named no script syntax there).
    if not _platform_supported(tools, name):
        return []
    return [tool for tool in tools["required"]
            if not (name == "win32" and tool in tools["script_tools"])]


#: Set by ``render`` so ``platform_tools`` can see the platform table of the
#: document it is rendering. Declared next to the constants above.
def _platform_supported(tools: Dict[str, Any], name: str) -> bool:
    spec = _PLATFORMS.get(name)
    return bool(isinstance(spec, dict) and spec.get("supported") is True)


_V1_CACHE: Dict[str, Any] = {}
_EFFECTIVE_CACHE: Dict[int, Dict[str, Any]] = {}


def _v1_limits() -> Dict[str, Any]:
    """v1's limits, read from the document rather than restated here."""
    if not _V1_CACHE:
        path = os.path.join(REPO, "contracts", "desktop", "v1.json")
        with open(path, "r", encoding="utf-8") as handle:
            _V1_CACHE.update(json.load(handle)["limits"])
    return _V1_CACHE


def effective_limit_values(major: int = DEFAULT_MAJOR) -> Dict[str, Any]:
    """Every bound in force at ``major``, with each value read once.

    The merge walks the chain from v1 upward, so v3 inherits v2's effective set
    (which already folds in v1's) without restating any of it.
    """
    if major in _EFFECTIVE_CACHE:
        return _EFFECTIVE_CACHE[major]
    with open(contract_path(1), "r", encoding="utf-8") as handle:
        merged: Dict[str, Any] = dict(json.load(handle)["limits"])
    for level in range(2, major + 1):
        with open(contract_path(level), "r", encoding="utf-8") as handle:
            document = json.load(handle)
        for key, value in (document.get("limits") or {}).items():
            if key in _LIMIT_DOC_KEYS:
                continue
            merged[key] = value
    _EFFECTIVE_CACHE[major] = merged
    return merged


def load_contract(major: int = DEFAULT_MAJOR) -> Dict[str, Any]:
    with open(contract_path(major), "r", encoding="utf-8") as handle:
        return json.load(handle)


def build(major: int = DEFAULT_MAJOR) -> str:
    return render(load_contract(major), major)

def _check_one(major: int) -> List[str]:
    """The staleness report for one major: empty when the file is current."""
    rendered = build(major)
    path = output_path(major)
    current = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            current = handle.read()
    if current == rendered:
        return []
    return ["%s is stale; run the generator"
            % os.path.basename(path)]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail when a committed file is stale")
    parser.add_argument("--version", type=int, default=None,
                        help="one protocol major; default is every known major")
    args = parser.parse_args(argv)
    majors = (args.version,) if args.version else PROTOCOL_MAJORS
    unknown = [major for major in majors if major not in PROTOCOL_MAJORS]
    if unknown:
        parser.error("unknown protocol major(s): %s"
                     % ", ".join(str(major) for major in unknown))
    if args.check:
        problems: List[str] = []
        for major in majors:
            problems.extend(_check_one(major))
        for problem in problems:
            sys.stderr.write(problem + "\n")
        return 1 if problems else 0
    for major in majors:
        with open(output_path(major), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(build(major))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
