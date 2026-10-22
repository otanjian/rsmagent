/**
 * Client-side rules for the v2 project-execution contract
 * (change ``align-desktop-project-execution-with-master``, task 6.1).
 *
 * The *values* -- protocol, phases, tools, limits, forbidden fields -- are
 * generated from ``contracts/desktop/v2.json`` into
 * ``./generated-contract.ts`` and re-exported here, so a phase name or a timeout
 * changes in exactly one place. This module holds only what cannot be generated:
 * the checks that decide whether a frame may be sent at all, how a received
 * state projects onto a phase, and how a v2 server is negotiated.
 *
 * Two rules from the contract live here rather than at a call site, because
 * they are the ones that must not be forgotten:
 *
 * * a frame this client *sends* is checked with the same rules the server
 *   applies -- no cwd, no module path, no credential, a known tool, a bounded
 *   timeout;
 * * an absent or mismatched v2 on either end means "unavailable", never "fall
 *   back to v1's read-only ops".
 *
 * Kept as plain Node (no Electron import) so ``node --test`` can drive it.
 */

import { createHash } from 'node:crypto'

import * as v2Contract from './generated-contract'
import * as v3Contract from './generated-contract-v3'

import {
    ARTIFACT_PROTOCOL,
    CANCELLING_OVERRIDES,
    CAPABILITY_REASONS,
    EFFECTFUL_TOOLS,
    EFFECTS_BY_ERROR_CODE,
    EFFECTS_BY_PHASE,
    EXECUTION_CAPABILITY_KEY,
    EXECUTION_EFFECTS,
    EXECUTION_FRAME_TYPES,
    EXECUTION_LIMITS,
    EXECUTION_PHASES,
    EXECUTION_TOOLS,
    EXECUTE_TOOL_OPTIONAL,
    EXECUTE_TOOL_REQUIRED,
    FORBIDDEN_FRAME_FIELDS,
    OUTCOME_UNKNOWN_CODE,
    PLATFORM_SUPPORT,
    PLATFORM_TOOLS,
    PROJECT_EXECUTION_NAME,
    PROJECT_EXECUTION_PROTOCOL,
    READONLY_TOOLS,
    SCRIPT_TOOLS,
    START_PERMIT_SINGLE_USE,
    START_PERMIT_TTL_SECONDS,
    STATE_TO_PHASE,
    TERMINAL_PHASES,
    TOOL_SCHEMA_VERSION,
} from './generated-contract'

/**
 * The generated contract for one protocol major, or ``null`` for one this build
 * cannot serve.
 *
 * The single place the desktop side decides "which rules apply to this peer",
 * mirroring ``execution_payload.contract_for_major`` on the server and
 * ``contract_module_for`` on the remote-device path. Every reader that reaches for
 * the v2 constants directly -- frame validation, negotiation, the tool set, the
 * digest field list -- goes through here instead, because the failure mode of not
 * doing so is exactly what happened: a v3 frame was judged by v2's rules and
 * refused for naming a major this code had never been taught.
 *
 * ``null`` rather than a v2 default is deliberate. A peer this build cannot serve
 * has to be told so; quietly judging it by the wrong table is how a
 * version-awareness gap stays invisible, and it is also how two ends would come to
 * disagree about a digest.
 */
export function contractForMajor(
    major: unknown,
): { readonly major: number; readonly mod: typeof v3Contract } | null {
    const value = Number(major)
    if (!Number.isInteger(value)) return null
    if (value === v2Contract.PROJECT_EXECUTION_PROTOCOL.major) {
        // Same shape by construction: one generator emits both modules.
        return { major: value, mod: v2Contract as unknown as typeof v3Contract }
    }
    if (value === v3Contract.PROJECT_EXECUTION_PROTOCOL.major) {
        return { major: value, mod: v3Contract }
    }
    return null
}

/** The majors this build can serve, highest first. */
export const SUPPORTED_EXECUTION_MAJORS: readonly number[] = [
    v3Contract.PROJECT_EXECUTION_PROTOCOL.major,
    v2Contract.PROJECT_EXECUTION_PROTOCOL.major,
]

/**
 * The v3 operation a tool spends, or ``undefined`` for a tool no contract knows.
 *
 * Generated from the contract's ``required_for`` table rather than restated, so
 * the mapping cannot drift from the document both ends check against. It exists so
 * a caller holding "this project is open" can ask the precise question -- *may
 * this tool run here* -- instead of the approximate one ("is the purpose
 * project-execution"), which cannot tell a file edit from running code.
 */
export function requiredOperationForTool(tool: unknown): string | undefined {
    if (typeof tool !== 'string') return undefined
    const table = v3Contract.TOOL_REQUIRED_OPERATION as Readonly<Record<string, string>>
    return table[tool]
}

/**
 * Whether an authorized operation set covers one tool.
 *
 * An empty/absent set covers nothing: the contract reads it as "no v3 operation
 * was authorized", never as "unrestricted".
 */
export function coversTool(
    operations: readonly string[] | undefined,
    tool: unknown,
): boolean {
    const needed = requiredOperationForTool(tool)
    if (!needed) return false
    return Array.isArray(operations) && operations.includes(needed)
}

export {
    ARTIFACT_PROTOCOL,
    CANCELLING_OVERRIDES,
    CAPABILITY_REASONS,
    EFFECTFUL_TOOLS,
    EFFECTS_BY_ERROR_CODE,
    EFFECTS_BY_PHASE,
    EXECUTION_CAPABILITY_KEY,
    EXECUTION_EFFECTS,
    EXECUTION_FRAME_TYPES,
    EXECUTION_LIMITS,
    EXECUTION_PHASES,
    EXECUTION_TOOLS,
    EXECUTE_TOOL_OPTIONAL,
    EXECUTE_TOOL_REQUIRED,
    FORBIDDEN_FRAME_FIELDS,
    OUTCOME_UNKNOWN_CODE,
    PLATFORM_SUPPORT,
    PLATFORM_TOOLS,
    PROJECT_EXECUTION_NAME,
    PROJECT_EXECUTION_PROTOCOL,
    READONLY_TOOLS,
    SCRIPT_TOOLS,
    START_PERMIT_SINGLE_USE,
    START_PERMIT_TTL_SECONDS,
    STATE_TO_PHASE,
    TERMINAL_PHASES,
    TOOL_SCHEMA_VERSION,
}

export type {
    ExecutionArtifact,
    ExecutionCapability,
    ExecutionEffects,
    ExecutionPhase,
    ExecutionResultFrame,
} from './generated-contract'

import type {
    ExecutionCapability,
    ExecutionEffects,
    ExecutionPhase,
} from './generated-contract'

/** The phase a command state maps to, or null when the state is unknown. */
export function phaseFor(
    state: string,
    options: { cancelRequested?: boolean; errorCode?: string | null } = {},
): ExecutionPhase | null {
    const base = STATE_TO_PHASE[state]
    if (!base) return null
    if (options.cancelRequested && CANCELLING_OVERRIDES.includes(base)) return 'cancelling'
    if (state === 'failed' && options.errorCode === OUTCOME_UNKNOWN_CODE) return 'outcome_unknown'
    return base
}

export function isTerminalPhase(phase: string): boolean {
    return (TERMINAL_PHASES as readonly string[]).includes(phase)
}

/**
 * What a terminal phase may claim. Never optimistic: a failure with no specific
 * code says `unknown`, and a cancelled run never claims a rollback.
 */
export function effectsFor(phase: string, errorCode?: string | null): ExecutionEffects | null {
    if (errorCode && EFFECTS_BY_ERROR_CODE[errorCode]) return EFFECTS_BY_ERROR_CODE[errorCode]
    if (EFFECTS_BY_PHASE[phase]) return EFFECTS_BY_PHASE[phase]
    if (phase === 'succeeded') return 'completed'
    if (phase === 'failed' || phase === OUTCOME_UNKNOWN_CODE) return 'unknown'
    return isTerminalPhase(phase) ? null : 'none'
}

/**
 * Whether a server's offered protocol is one this build can serve.
 *
 * ``false`` means the entry point is not offered -- and the caller must say so
 * rather than fall back to a v1 op.
 *
 * Accepting more than one major is the change: previously only v2 was reachable,
 * so a server that offered v3 was answered ``protocol_incompatible`` and the whole
 * v3 mechanism (the generated contract, ``operations``, ``expected_version``) had
 * no way in. ``contractForMajor`` is what decides, so the accepted set and the
 * rules applied later cannot disagree -- a peer admitted here and then judged by
 * another major's table is the specific bug this replaces.
 *
 * The chosen major is returned so a caller does not have to re-derive it: reading
 * it twice is how the frame and the negotiation would drift apart.
 */
export function negotiateProjectExecution(
    protocols: Record<string, { major?: number }> | null | undefined,
): { available: boolean; reason: string; major?: number } {
    const offered = protocols ? protocols[PROJECT_EXECUTION_NAME] : undefined
    if (!offered || typeof offered.major !== 'number') {
        return { available: false, reason: 'not_implemented' }
    }
    const contract = contractForMajor(offered.major)
    if (!contract) {
        return { available: false, reason: 'protocol_incompatible' }
    }
    return { available: true, reason: 'available', major: contract.major }
}

/** Read the optional capability block out of a meta/hello payload. */
export function parseExecutionCapability(meta: unknown): ExecutionCapability | null {
    if (!meta || typeof meta !== 'object') return null
    const block = (meta as Record<string, unknown>)[EXECUTION_CAPABILITY_KEY]
    if (!block || typeof block !== 'object') return null
    const candidate = block as Record<string, unknown>
    if (typeof candidate.available !== 'boolean') return null
    if (typeof candidate.protocol_major !== 'number') return null
    return candidate as unknown as ExecutionCapability
}

/** A digest shape check that mirrors the server's, so both sides agree. */
export function looksLikeDigest(value: unknown): boolean {
    if (typeof value !== 'string' || !value.startsWith('sha256:')) return false
    const body = value.slice('sha256:'.length)
    return body.length >= 16 && /^[0-9a-fA-F]+$/.test(body)
}

/** The envelope fields the digest covers, in the contract's order. */
export const DIGEST_FIELDS = [
    'tool',
    'tool_schema_version',
    'arguments',
    'run_id',
    'tool_call_id',
    'session_id',
    'agent_id',
    'origin',
    'binding_id',
    'workspace_id',
    'device_id',
    'grant_version',
    'selection_generation',
    'skill_resources',
] as const

/** Compare two strings by Unicode code point, as Python's ``sorted`` does. */
function byCodePoint(a: string, b: string): number {
    const left = Array.from(a)
    const right = Array.from(b)
    for (let i = 0; i < Math.min(left.length, right.length); i += 1) {
        const diff = left[i].codePointAt(0)! - right[i].codePointAt(0)!
        if (diff !== 0) return diff
    }
    return left.length - right.length
}

/**
 * Canonical JSON: object keys sorted by code point, no insignificant
 * whitespace, UTF-8. The Python side does the same with
 * ``json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=False)``.
 *
 * A non-integer number is refused rather than serialized: ``1.0`` and ``1``
 * (and ``Infinity``) can differ between the two languages, and two different
 * digests for one command is exactly the confusion the digest prevents.
 */
export function canonicalJson(value: unknown, path = 'envelope'): string {
    if (value === null) return 'null'
    if (typeof value === 'string') return JSON.stringify(value)
    if (typeof value === 'boolean') return value ? 'true' : 'false'
    if (typeof value === 'number') {
        if (!Number.isInteger(value) || !Number.isFinite(value)) {
            throw new Error(`${path} must not contain a non-integer number`)
        }
        return JSON.stringify(value)
    }
    if (Array.isArray(value)) {
        return `[${value.map((item, index) => canonicalJson(item, `${path}[${index}]`)).join(',')}]`
    }
    if (value && typeof value === 'object') {
        const keys = Object.keys(value as Record<string, unknown>).sort(byCodePoint)
        const parts = keys.map((key) => {
            if (key === '__proto__') throw new Error(`${path} must not use '__proto__'`)
            return `${JSON.stringify(key)}:${canonicalJson(
                (value as Record<string, unknown>)[key], `${path}.${key}`)}`
        })
        return `{${parts.join(',')}}`
    }
    throw new Error(`${path} contains a non-JSON value`)
}

/**
 * The canonical envelope: only the covered fields, absent optionals normalised
 * to ``null``/``[]`` so "no skills" and "skills forgotten" cannot collide.
 */
export function canonicalEnvelope(envelope: Record<string, unknown>): Record<string, unknown> {
    const out: Record<string, unknown> = {}
    for (const key of DIGEST_FIELDS) {
        let value = envelope[key]
        if (key === 'skill_resources') {
            const entries = (Array.isArray(value) ? value : [])
                .map((entry) => ({
                    skill_id: (entry as Record<string, unknown>)?.skill_id ?? null,
                    digest: (entry as Record<string, unknown>)?.digest ?? null,
                }))
                .sort((a, b) => byCodePoint(String(a.skill_id), String(b.skill_id)))
            value = entries
        }
        out[key] = value === undefined ? null : value
    }
    // Serialize once here as well: a value that cannot be canonicalized must
    // fail before a digest is computed from it.
    canonicalJson(out)
    return out
}

/**
 * ``sha256:<hex>`` over the canonical envelope, byte-identical to the server's
 * ``integrations.desktop.execution_payload.params_digest``. The shared fixture
 * ``contracts/desktop/samples/v2/digest_envelope.valid.json`` pins it.
 */
export function paramsDigest(envelope: Record<string, unknown>): string {
    return `sha256:${createHash('sha256').update(
        canonicalJson(canonicalEnvelope(envelope)), 'utf8').digest('hex')}`
}

function isPositiveInt(value: unknown): value is number {
    return typeof value === 'number' && Number.isInteger(value) && value > 0
}

/**
 * The problems that make a frame unsendable, judged by the **negotiated** major.
 *
 * ``agreedMajor`` is what this connection settled on, not what the frame claims:
 * a frame does not get to choose which rules judge it. Letting it would mean the
 * looser contract could be selected by the frame itself -- a v3 frame sent over a
 * v2 connection would be checked against v3 and pass, which is the whole class of
 * mistake this pair of ends has already made once. The frame's own
 * ``protocol_major`` is then required to *match* what was agreed, so a mismatch is
 * reported rather than silently tolerated.
 *
 * Callers that have not negotiated pass nothing and keep v2, which is what every
 * existing caller is.
 */
export function validateExecuteFrame(
    frame: unknown,
    platform: 'posix' | 'win32' = 'posix',
    agreedMajor?: number,
): string[] {
    if (!frame || typeof frame !== 'object') return ['execute_tool frame is not an object']
    const f = frame as Record<string, unknown>
    // ``undefined`` means v2, the protocol every existing caller speaks -- **not**
    // "ask the frame". A frame choosing its own rules is the flaw this signature
    // exists to remove, so it must not reappear as a default: a v3 frame on a v2
    // connection would then be judged by v3 and pass. A caller that has negotiated
    // a major passes it; one that has not gets v2, which is what it means.
    const contract = contractForMajor(agreedMajor ?? 2)
    if (!contract) {
        return [`protocol_major must be one of ${SUPPORTED_EXECUTION_MAJORS.join(', ')}`
            + ` (got ${JSON.stringify(agreedMajor ?? 2)})`]
    }
    return validateFrameAgainst(contract.mod, f, platform)
}

/**
 * The shared frame check, parameterised by the generated contract.
 *
 * One implementation for both majors on purpose. The structural rules are the same
 * shape in each contract -- required fields, forbidden fields, a tool allow-list,
 * per-tool arguments -- so writing them twice would guarantee they drift, and drift
 * is exactly how this pair of ends came to disagree about a digest before. What is
 * *not* shared is the content: ``mod`` supplies every constant.
 */
function validateFrameAgainst(
    mod: typeof v3Contract,
    f: Record<string, unknown>,
    platform: 'posix' | 'win32',
): string[] {
    const problems: string[] = []
    if (f.type !== 'execute_tool') problems.push("frame type is not 'execute_tool'")
    if (f.protocol_major !== mod.PROJECT_EXECUTION_PROTOCOL.major) {
        problems.push(`protocol_major must be ${mod.PROJECT_EXECUTION_PROTOCOL.major}`)
    }
    // The numeric fields first: they have no non-empty-string shape to check.
    if (!isPositiveInt(f.grant_version)) {
        problems.push('grant_version must be a positive integer')
    }
    if (typeof f.selection_generation !== 'number'
        || !Number.isInteger(f.selection_generation) || f.selection_generation < 0) {
        problems.push('selection_generation must be a non-negative integer')
    }
    if (f.tool_schema_version !== mod.TOOL_SCHEMA_VERSION) {
        problems.push(`tool_schema_version must be ${mod.TOOL_SCHEMA_VERSION}`)
    }
    for (const key of mod.EXECUTE_TOOL_REQUIRED) {
        if (key === 'type' || key === 'protocol_major' || key === 'grant_version'
            || key === 'selection_generation' || key === 'tool_schema_version') {
            continue
        }
        const value = f[key]
        if (value === undefined || value === null || value === '') {
            problems.push(`execute_tool is missing '${key}'`)
        }
    }
    for (const key of mod.FORBIDDEN_FRAME_FIELDS) {
        if (key in f) problems.push(`execute_tool must not carry '${key}'`)
    }
    const tool = f.tool
    if (typeof tool !== 'string'
        || !(mod.EXECUTION_TOOLS as readonly string[]).includes(tool)) {
        problems.push(`unknown tool '${String(tool)}'`)
        return problems
    }
    if (!mod.PLATFORM_SUPPORT[platform]) {
        // No accepted launcher on this platform yet: refuse by name instead of
        // translating a POSIX command into a shell the user never approved.
        problems.push(`tool '${tool}' is unsupported on platform '${platform}'`)
    } else {
        // v3 admits win32 as a *platform* while offering it only some tools
        // (``launcher_kinds.scripts`` is empty there), so platform admission is not
        // enough: the tool has to be one this platform actually has. Without this,
        // a v3 frame could name ``bash`` on Windows and pass -- the platform says
        // yes and nothing asks about the tool. v2 has no per-platform tool table,
        // so this is a no-op for it and its answers cannot change.
        const offered = mod.PLATFORM_TOOLS[platform] as readonly string[] | undefined
        if (Array.isArray(offered) && !offered.includes(tool)) {
            problems.push(`tool '${tool}' is unsupported on platform '${platform}'`)
        }
    }
    if (!looksLikeDigest(f.params_digest)) {
        problems.push('params_digest must be a sha256: prefixed digest')
    }
    const args = f.arguments
    if (!args || typeof args !== 'object' || Array.isArray(args)) {
        problems.push('arguments must be an object')
    } else {
        const a = args as Record<string, unknown>
        for (const key of mod.FORBIDDEN_FRAME_FIELDS) {
            if (key in a) problems.push(`execute_tool.arguments must not carry '${key}'`)
        }
        if ((mod.SCRIPT_TOOLS as readonly string[]).includes(tool)) {
            // The two contracts spell a script differently and this asks which,
            // rather than assuming v2's shape:
            //
            // * ``bash`` keeps ``command`` (a POSIX shell string) in both;
            // * ``run_script`` -- v3 only -- splits that into ``language`` and
            //   ``script``, precisely so the runtime is a declared fact rather than
            //   something inferred from the text.
            //
            // v2's path is byte-for-byte what it was: same field, same message.
            const required: readonly string[] = tool === 'run_script'
                ? ['language', 'script']
                : ['command']
            for (const key of required) {
                const value = a[key]
                if (typeof value !== 'string' || value.trim() === '') {
                    problems.push(`${tool}.arguments.${key} must be a non-empty string`)
                }
            }
        }
        if (a.timeout !== undefined) {
            if (!isPositiveInt(a.timeout)) {
                problems.push(`${tool}.arguments.timeout must be a positive integer`)
            } else if (a.timeout > mod.EXECUTION_LIMITS.script_timeout_max_seconds) {
                problems.push(
                    `${tool}.arguments.timeout must be <= `
                    + `${mod.EXECUTION_LIMITS.script_timeout_max_seconds} seconds`,
                )
            }
        }
    }
    return problems
}

/**
 * The tools this client may claim on its own platform. Used by the hello
 * payload, so the server intersects a *true* declaration with the user's scope.
 */
export function supportedTools(platform: 'posix' | 'win32'): string[] {
    return [...(PLATFORM_TOOLS[platform] || [])]
}
