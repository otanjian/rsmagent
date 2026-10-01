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
 * Whether a v2 server is reachable. `false` means the entry point is not
 * offered -- and the caller must say so rather than fall back to a v1 op.
 */
export function negotiateProjectExecution(
    protocols: Record<string, { major?: number }> | null | undefined,
): { available: boolean; reason: string } {
    const offered = protocols ? protocols[PROJECT_EXECUTION_NAME] : undefined
    if (!offered || typeof offered.major !== 'number') {
        return { available: false, reason: 'not_implemented' }
    }
    if (offered.major !== PROJECT_EXECUTION_PROTOCOL.major) {
        return { available: false, reason: 'protocol_incompatible' }
    }
    return { available: true, reason: 'available' }
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

/** The problems that make a frame unsendable, mirroring the server's checks. */
export function validateExecuteFrame(
    frame: unknown,
    platform: 'posix' | 'win32' = 'posix',
): string[] {
    const problems: string[] = []
    if (!frame || typeof frame !== 'object') return ['execute_tool frame is not an object']
    const f = frame as Record<string, unknown>
    if (f.type !== 'execute_tool') problems.push("frame type is not 'execute_tool'")
    if (f.protocol_major !== PROJECT_EXECUTION_PROTOCOL.major) {
        problems.push(`protocol_major must be ${PROJECT_EXECUTION_PROTOCOL.major}`)
    }
    // The numeric fields first: they have no non-empty-string shape to check.
    if (!isPositiveInt(f.grant_version)) {
        problems.push('grant_version must be a positive integer')
    }
    if (typeof f.selection_generation !== 'number'
        || !Number.isInteger(f.selection_generation) || f.selection_generation < 0) {
        problems.push('selection_generation must be a non-negative integer')
    }
    if (f.tool_schema_version !== TOOL_SCHEMA_VERSION) {
        problems.push(`tool_schema_version must be ${TOOL_SCHEMA_VERSION}`)
    }
    for (const key of EXECUTE_TOOL_REQUIRED) {
        if (key === 'type' || key === 'protocol_major' || key === 'grant_version'
            || key === 'selection_generation' || key === 'tool_schema_version') {
            continue
        }
        const value = f[key]
        if (value === undefined || value === null || value === '') {
            problems.push(`execute_tool is missing '${key}'`)
        }
    }
    for (const key of FORBIDDEN_FRAME_FIELDS) {
        if (key in f) problems.push(`execute_tool must not carry '${key}'`)
    }
    const tool = f.tool
    if (typeof tool !== 'string' || !(EXECUTION_TOOLS as readonly string[]).includes(tool)) {
        problems.push(`unknown tool '${String(tool)}'`)
        return problems
    }
    if (!PLATFORM_SUPPORT[platform]) {
        // No accepted launcher on this platform yet: refuse by name instead of
        // translating a POSIX command into a shell the user never approved.
        problems.push(`tool '${tool}' is unsupported on platform '${platform}'`)
    }
    if (!looksLikeDigest(f.params_digest)) {
        problems.push('params_digest must be a sha256: prefixed digest')
    }
    const args = f.arguments
    if (!args || typeof args !== 'object' || Array.isArray(args)) {
        problems.push('arguments must be an object')
    } else {
        const a = args as Record<string, unknown>
        for (const key of FORBIDDEN_FRAME_FIELDS) {
            if (key in a) problems.push(`execute_tool.arguments must not carry '${key}'`)
        }
        if ((SCRIPT_TOOLS as readonly string[]).includes(tool)) {
            if (typeof a.command !== 'string' || a.command.trim() === '') {
                problems.push(`${tool}.arguments.command must be a non-empty string`)
            }
            if (a.timeout !== undefined) {
                if (!isPositiveInt(a.timeout)) {
                    problems.push(`${tool}.arguments.timeout must be a positive integer`)
                } else if (a.timeout > EXECUTION_LIMITS.script_timeout_max_seconds) {
                    problems.push(
                        `${tool}.arguments.timeout must be <= ${EXECUTION_LIMITS.script_timeout_max_seconds} seconds`,
                    )
                }
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
