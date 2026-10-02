#!/usr/bin/env python
# encoding:utf-8
"""Generate the desktop client's v2 contract constants from the contract document.

Change ``align-desktop-project-execution-with-master``, task 6.1.

``contracts/desktop/v2.json`` is the single source; this script turns it into
``desktop/src/main/project-execution/generated-contract.ts`` so the client does
not keep a second hand-written copy of the phase table, the tool list or the
limits. ``tests/test_desktop_execution_types.py`` re-runs the generator and fails
if the committed file is stale, which is what makes "generated" a fact rather
than a comment.

Usage:
    .venv/bin/python scripts/gen_desktop_execution_types.py          # write
    .venv/bin/python scripts/gen_desktop_execution_types.py --check  # verify
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(REPO, "contracts", "desktop", "v2.json")
OUTPUT = os.path.join(REPO, "desktop", "src", "main", "project-execution",
                     "generated-contract.ts")

HEADER = """/**
 * GENERATED FILE -- do not edit.
 *
 * Source: contracts/desktop/v2.json (project-execution protocol, major 2).
 * Regenerate: .venv/bin/python scripts/gen_desktop_execution_types.py
 * Checked by: tests/test_desktop_execution_types.py (fails when stale)
 *
 * Behaviour lives in ./contract.ts; only values and types are generated here,
 * so a phase, a tool or a limit changes in exactly one place.
 */

"""


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


def render(contract: Dict[str, Any]) -> str:
    protocol = contract["protocol"]
    tools = contract["tools"]
    frames = contract["frames"]
    phases = contract["phases"]
    effects = contract["effects"]
    limits = contract["limits"]
    platforms = contract["platforms"]
    capability = contract["capability"]
    text = [HEADER]

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

    text.append("/** Bounds this client enforces locally, using the v1 values v2 reuses. */\n")
    text.append("export const EXECUTION_LIMITS = {\n")
    for key in sorted(_limit_keys(contract)):
        text.append("    %s: %s,\n" % (key, _limit_value(limits, key, contract)))
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
        offered = [] if not platforms[name]["supported"] else [
            tool for tool in tools["required"]
            if not (name == "win32" and tool in tools["script_tools"])]
        text.append("    %s: %s,\n" % (_key(name), _ts_string_list(offered)))
    text.append("}\n\n")

    text.append("/** Permits are single use and short lived. */\n")
    text.append("export const START_PERMIT_TTL_SECONDS = %s\n"
                % _limit_value(limits, "start_permit_ttl_seconds", contract))
    text.append("export const START_PERMIT_SINGLE_USE = %s\n\n"
                % ("true" if limits["start_permit_single_use"] else "false"))

    text.append("/** The artifact protocol this client produces references for. */\n")
    text.append("export const ARTIFACT_PROTOCOL = '%s'\n\n" % capability["artifact_protocol"])
    text.append("/** The meta/hello key this capability is reported under. */\n")
    text.append("export const EXECUTION_CAPABILITY_KEY = '%s'\n\n"
                % contract["meta_additions"]["keys"][0])
    text.append("/** Reasons a capability may report instead of 'available'. */\n")
    text.append("export const CAPABILITY_REASONS = %s\n\n"
                % _ts_literal_list(capability["reason_values"]))

    journal = contract["journal"]
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


def _limit_keys(contract: Dict[str, Any]) -> List[str]:
    """Every limit the client needs, including the ones inherited from v1."""
    inherited = {
        "ws_frame_max_bytes", "heartbeat_seconds", "offline_seconds",
        "parallel_commands_per_device", "pending_queue", "offline_wait_seconds",
        "transfer_chunk_max_bytes",
    }
    declared = [key for key in contract["limits"]
                if key not in ("note", "reuse_v1", "advisory", "advisory_note")]
    return sorted(set(declared) | {key for key in contract["limits"]["reuse_v1"]
                                   if key in inherited})


def _limit_value(limits: Dict[str, Any], key: str, contract: Dict[str, Any]) -> str:
    raw = limits[key] if key in limits else _v1_limits()[key]
    if isinstance(raw, bool):
        return "true" if raw else "false"
    return str(raw)


_V1_CACHE: Dict[str, Any] = {}


def _v1_limits() -> Dict[str, Any]:
    """v1's limits, read from the document rather than restated here."""
    if not _V1_CACHE:
        path = os.path.join(REPO, "contracts", "desktop", "v1.json")
        with open(path, "r", encoding="utf-8") as handle:
            _V1_CACHE.update(json.load(handle)["limits"])
    return _V1_CACHE


def load_contract() -> Dict[str, Any]:
    with open(CONTRACT, "r", encoding="utf-8") as handle:
        return json.load(handle)


def build() -> str:
    return render(load_contract())


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail when the committed file is stale")
    args = parser.parse_args(argv)
    rendered = build()
    if args.check:
        current = ""
        if os.path.exists(OUTPUT):
            with open(OUTPUT, "r", encoding="utf-8") as handle:
                current = handle.read()
        if current != rendered:
            sys.stderr.write("generated-contract.ts is stale; run the generator\n")
            return 1
        return 0
    with open(OUTPUT, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
