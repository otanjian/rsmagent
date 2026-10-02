/**
 * GENERATED FILE -- do not edit.
 *
 * Source: contracts/desktop/v2.json (project-execution protocol, major 2).
 * Regenerate: .venv/bin/python scripts/gen_desktop_execution_types.py
 * Checked by: tests/test_desktop_execution_types.py (fails when stale)
 *
 * Behaviour lives in ./contract.ts; only values and types are generated here,
 * so a phase, a tool or a limit changes in exactly one place.
 */

/** The v2 protocol this client speaks. Optional: v1-only peers keep working. */
export const PROJECT_EXECUTION_PROTOCOL = {
    major: 2,
    minor: 0,
    required: false,
} as const

export const PROJECT_EXECUTION_NAME = 'project_execution'

/** Frame types v2 adds on top of v1's gateway frames. */
export const EXECUTION_FRAME_TYPES = [
    'execute_tool',
    'execution_heartbeat',
    'execution_status',
    'execution_result',
    'execution_cancelled',
    'execution_start_permit',
] as const

/** Fields an execute_tool frame must carry. */
export const EXECUTE_TOOL_REQUIRED = [
    'type',
    'protocol_major',
    'command_id',
    'run_id',
    'tool_call_id',
    'binding_id',
    'workspace_id',
    'device_id',
    'grant_version',
    'selection_generation',
    'connection_epoch',
    'tool',
    'tool_schema_version',
    'arguments',
    'params_digest',
    'expires_at',
] as const

/** Fields an execute_tool frame may carry. */
export const EXECUTE_TOOL_OPTIONAL = [
    'session_id',
    'agent_id',
    'skill_resources',
    'deadline_seconds',
    'background',
    'approval_id',
    'permission_mode',
    'origin',
] as const

/** Fields an execute_tool frame must never carry. */
export const FORBIDDEN_FRAME_FIELDS = [
    'cwd',
    'root_path',
    'absolute_path',
    'env',
    'server_url',
    'authorization',
    'token',
    'module',
    'class_name',
    'argv_path',
] as const

/** The master tool names a v2 frame may carry. */
export const EXECUTION_TOOLS = [
    'read',
    'write',
    'edit',
    'ls',
    'search_files',
    'bash',
] as const

export const EFFECTFUL_TOOLS = [
    'write',
    'edit',
    'bash',
] as const

export const READONLY_TOOLS = [
    'read',
    'ls',
    'search_files',
] as const

export const SCRIPT_TOOLS = [
    'bash',
] as const

/** The frozen argument shape these tools belong to. */
export const TOOL_SCHEMA_VERSION = 1

/** The visible phases -- projected, never stored as a state. */
export const EXECUTION_PHASES = [
    'queued',
    'preparing',
    'running',
    'succeeded',
    'failed',
    'cancelling',
    'cancelled',
    'expired',
    'outcome_unknown',
] as const

export type ExecutionPhase = (typeof EXECUTION_PHASES)[number]

/** Terminal phases: the result has arrived and will not change. */
export const TERMINAL_PHASES: readonly ExecutionPhase[] = ['succeeded', 'failed', 'cancelled', 'expired', 'outcome_unknown']

/** The server command states this client may see, mapped to their phase. */
export const STATE_TO_PHASE: Readonly<Record<string, ExecutionPhase>> = {
    acknowledged: 'preparing',
    cancelled: 'cancelled',
    dispatched: 'preparing',
    expired: 'expired',
    failed: 'failed',
    queued: 'queued',
    running: 'running',
    succeeded: 'succeeded',
}

/** Phases a cancellation request changes the visible phase of. */
export const CANCELLING_OVERRIDES: readonly ExecutionPhase[] = ['queued', 'preparing', 'running']

/** The error code that means 'may have written, no conclusion'. */
export const OUTCOME_UNKNOWN_CODE = 'outcome_unknown'

/** What a terminal result may claim about the project directory. */
export const EXECUTION_EFFECTS = [
    'none',
    'partial',
    'completed',
    'unknown',
] as const

export type ExecutionEffects = (typeof EXECUTION_EFFECTS)[number]

/** The default effect per phase (before any error code narrows it). */
export const EFFECTS_BY_PHASE: Readonly<Record<string, ExecutionEffects>> = {
    cancelled: 'unknown',
    expired: 'none',
    preparing: 'none',
    queued: 'none',
    running: 'none',
}

/** The effect a specific error code proves. */
export const EFFECTS_BY_ERROR_CODE: Readonly<Record<string, ExecutionEffects>> = {
    deadline_exceeded: 'partial',
    dependency_missing: 'none',
    device_offline: 'none',
    file_changed: 'partial',
    grant_revoked: 'none',
    incompatible_skill: 'none',
    limit_exceeded: 'none',
    outcome_unknown: 'unknown',
    permission_denied: 'none',
    resource_unavailable: 'none',
    runtime_unavailable: 'none',
    stale_context: 'none',
}

/** Bounds this client enforces locally, using the v1 values v2 reuses. */
export const EXECUTION_LIMITS = {
    effectful_per_project: 1,
    heartbeat_seconds: 20,
    inline_stderr_max_bytes: 16384,
    inline_stdout_max_bytes: 32768,
    journal_retain_days: 7,
    offline_seconds: 60,
    offline_wait_seconds: 30,
    parallel_commands_per_device: 4,
    pending_queue: 32,
    preview_max_bytes: 16777216,
    readonly_parallel_per_project: 4,
    run_heartbeat_seconds: 10,
    run_liveness_seconds: 30,
    script_timeout_default_seconds: 120,
    script_timeout_max_seconds: 600,
    skill_package_expanded_max_bytes: 268435456,
    skill_package_files_max: 10000,
    skill_package_transfer_max_bytes: 67108864,
    start_permit_single_use: true,
    start_permit_ttl_seconds: 10,
    transfer_chunk_max_bytes: 4194304,
    worker_memory_bytes: 1073741824,
    ws_frame_max_bytes: 65536,
} as const

/** Whether the contract declares an accepted launcher per platform. */
export const PLATFORM_SUPPORT: Readonly<Record<string, boolean>> = {
    posix: true,
    win32: false,
}

/** Tools each supported platform offers (script tools excluded). */
export const PLATFORM_TOOLS: Readonly<Record<string, string[]>> = {
    posix: ['read', 'write', 'edit', 'ls', 'search_files', 'bash'],
    win32: [],
}

/** Permits are single use and short lived. */
export const START_PERMIT_TTL_SECONDS = 10
export const START_PERMIT_SINGLE_USE = true

/** The artifact protocol this client produces references for. */
export const ARTIFACT_PROTOCOL = 'local-artifact-v1'

/** The meta/hello key this capability is reported under. */
export const EXECUTION_CAPABILITY_KEY = 'project_execution'

/** Reasons a capability may report instead of 'available'. */
export const CAPABILITY_REASONS = [
    'available',
    'not_implemented',
    'not_accepted',
    'disabled_by_deployment',
    'platform_unsupported',
] as const

/** Fields a local start-journal record must carry (execution-contract §4). */
export const JOURNAL_REQUIRED = [
    'journal_id',
    'command_id',
    'params_digest',
    'origin',
    'device_id',
    'workspace_id',
    'run_id',
    'tool_call_id',
    'tool',
    'grant_version',
    'started_at',
] as const

/** Fields it may carry. */
export const JOURNAL_OPTIONAL = [
    'skill_digests',
    'connection_epoch',
    'permit_id',
] as const

/** The identity a redelivery is deduplicated against. */
export const JOURNAL_DEDUP_KEY = [
    'origin',
    'user_id',
    'tenant_id',
    'device_id',
    'command_id',
] as const

/** The code a same-id/different-payload redelivery is refused with. */
export const JOURNAL_CONFLICT_CODE = 'command_conflict'

/** One local start-journal record. Written before the worker can touch the project. */
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

export interface ExecutionCapability {
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
