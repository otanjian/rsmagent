/**
 * The device end of the v2 execution channel (tasks 7.1 - 7.7, and the wiring
 * the fault injections were written against).
 *
 * Everything below this file already existed as separate, tested pieces: the
 * journal decides whether a command may run at all, the command runner owns the
 * gate, the cancellation and the staleness check, and the local-execution
 * launcher owns the sandbox. What was missing was the join -- something that
 * takes one ``execute_tool`` frame off the device connection, produces the
 * ``execution_result`` frame the server's ``complete_execution`` accepts, and
 * refuses everything in between that is not a frame this device may run.
 *
 * The split of responsibility is deliberate:
 *
 * * **This file parses.** It is the only place that reads a raw frame, so the
 *   journal never sees an unvalidated payload and the runner never sees a
 *   workspace id it resolved itself.
 * * **The root is never read from the frame.** ``realRootOf`` is the device's own
 *   binding table; a frame naming a workspace this install has not bound is
 *   ``stale_context``, not "look it up somewhere else".
 * * **The identity is never read from the frame.** The user and tenant come from
 *   the device's own session, and the frame's ``device_id`` must agree with it --
 *   otherwise a frame could write into another account's journal scope.
 * * **No tool is executed here.** ``runTool`` is injected; production passes the
 *   sandboxed worker call, and the tests pass a real process.
 */

import {
    EXECUTION_LIMITS,
    PROJECT_EXECUTION_PROTOCOL,
    effectsFor,
    isTerminalPhase,
    validateExecuteFrame,
    type ExecutionArtifact,
    type ExecutionResultFrame,
} from './contract'
import {
    CommandRunner,
    type CommandResult,
    type DeviceIdentity,
    type ExecutionOutcome,
    type ExecutionRequest,
    type FramePaths,
    type Terminator,
} from './command-runner'
import { JournalStore, type JournalEntry } from './journal'
import { buildArtifact } from './artifact'
import { SkillCache, SkillCacheError, type DeclaredSkill } from './skill-cache'

/** The frame types this end answers. Anything else is not ours. */
export const DEVICE_FRAME_TYPES = ['execute_tool', 'execution_cancel', 'execution_status'] as const

/**
 * The skill versions a frame declares, normalised, or a refusal.
 *
 * ``undefined``/``null`` means "this run requires no skills" -- what every
 * skill-less tool sends, and not the same claim as an empty list from a frame
 * that was supposed to carry a set. An entry with no ``skill_id`` or no digest is
 * refused rather than dropped: dropping one would turn "run these two versions"
 * into "run this one", which is a narrower requirement than the server
 * authorized.
 */
export function declaredSkills(frame: Record<string, unknown>): DeclaredSkill[] {
    const raw = frame.skill_resources
    if (raw === undefined || raw === null) return []
    if (!Array.isArray(raw)) {
        throw new SkillCacheError('incompatible_skill', 'skill_resources 不是数组')
    }
    return raw.map((entry) => {
        if (!entry || typeof entry !== 'object') {
            throw new SkillCacheError('incompatible_skill', 'skill_resources 条目不是对象')
        }
        const record = entry as Record<string, unknown>
        const skillId = String(record.skill_id ?? '').trim()
        const digest = String(record.digest ?? '').trim()
        if (!skillId) {
            throw new SkillCacheError('incompatible_skill', '技能资源缺少 skill_id')
        }
        if (!digest) {
            throw new SkillCacheError(
                'incompatible_skill', `技能 ${skillId} 没有声明摘要，无法校验版本`)
        }
        return { skillId, digest }
    })
}

function codeOf(err: unknown, fallback: string): string {
    const code = (err as { code?: unknown })?.code
    return typeof code === 'string' && code ? code : fallback
}

function messageOf(err: unknown): string {
    return err instanceof Error ? err.message : String(err)
}

/** Tool arguments that name something on the project's disk. */
const CONTENT_ARGS = ['path', 'file_path', 'directory', 'dir'] as const

export interface DeviceExecutionOptions {
    /**
     * The device's own session for the frame's workspace, or ``null``.
     *
     * Resolved per frame from the bound workspace's *live* grant rather than
     * captured once: a sign-out must stop accepting frames immediately. The
     * origin comes from the connection, never from the frame.
     */
    identity: (frame: Record<string, unknown>) => DeviceIdentity | null
    /**
     * The real root of a *bound* project-execution workspace, or ``null``.
     *
     * Must be the device's own record (the grant table behind the binding), and
     * must collapse symlinks: the runner keys its serialisation on this string.
     */
    realRootOf: (workspaceId: string) => string | null
    /** Run one admitted frame's tool. The sandboxed worker call in production. */
    runTool: (request: ExecutionRequest) => Promise<ExecutionOutcome>
    /** Stop a run's process tree, resolving only once it has ended. */
    terminate: Terminator
    /** Where the durable journal lives (under the app's user-data directory). */
    journalFile: string
    platform?: NodeJS.Platform
    /** Whether the connection is live; ``false`` refuses new frames (A19). */
    connected?: () => boolean
    now?: () => number
    onReconcile?: (entry: JournalEntry, reason: string) => void
    newJournalId?: () => string
    /** The liveness window, in ms (defaults to the contract's own value). */
    livenessWindowMs?: number
    /**
     * The device's cache of verified skill versions (task 8.9), or ``null``.
     *
     * Needed to answer the one question this end must not take on trust: "do I
     * actually hold the skill versions this run was authorized with?". A frame
     * that declares skills and finds no cache here is refused rather than run --
     * a device that cannot prove which version it would run must not run one.
     */
    skillCache?: SkillCache | null
    /**
     * Obtain the bytes for skill versions this frame declares (task 8.9).
     *
     * Called before the cache check, and only when the frame declares skills.
     * This is an *attempt*, not an authority: what it installs is still verified
     * against the declared digest by the cache, and the cache check that follows
     * is what decides whether the run may proceed. A device without this hook
     * simply cannot obtain versions it does not already hold, which is the
     * correct answer for a build that offers no transfer.
     *
     * It throws for a refusal the run should report by name; ``execute`` turns
     * that into a result frame with the error's own code.
     */
    fetchSkills?: (frame: Record<string, unknown>,
                   declared: readonly DeclaredSkill[]) => Promise<void>
}

/**
 * The device's execution endpoint: one frame in, one result frame out.
 *
 * Kept free of Electron and of the worker, so the whole admission -> journal ->
 * gate -> result path is driven in a plain Node test with a real process.
 */
export class DeviceExecution {
    readonly runner: CommandRunner
    private readonly options: DeviceExecutionOptions

    constructor(options: DeviceExecutionOptions) {
        this.options = options
        const journal = new JournalStore({
            file: options.journalFile,
            ...(options.now ? { now: options.now } : {}),
        })
        this.runner = new CommandRunner({
            journal,
            realRootOf: options.realRootOf,
            execute: options.runTool,
            terminate: options.terminate,
            ...(options.now ? { now: options.now } : {}),
            ...(options.connected ? { connected: options.connected } : {}),
            ...(options.onReconcile ? { onReconcile: options.onReconcile } : {}),
            ...(options.newJournalId ? { newJournalId: options.newJournalId } : {}),
            ...(options.livenessWindowMs === undefined
                ? {} : { livenessWindowMs: options.livenessWindowMs }),
            pathsOf: (frame, realRoot) => this.pathsOf(frame, realRoot),
        })
    }

    /** Whether the journal could not be read, in which case nothing runs. */
    get degraded(): boolean {
        return this.runner.degraded
    }

    /**
     * Handle one frame and produce the frame to send back.
     *
     * The refusals before the runner are the ones that must not even reach the
     * journal: a frame with a credential in it, a frame for a platform this
     * install does not confine, or a frame whose device is not this one.
     */
    async execute(frame: Record<string, unknown>): Promise<ExecutionResultFrame> {
        const commandId = String(frame.command_id ?? '')
        const identity = this.options.identity(frame)
        if (!identity) {
            return this.refusal(frame, 'stale_context',
                'this device is not bound to a session')
        }
        const platform = (this.options.platform ?? process.platform) === 'win32' ? 'win32' : 'posix'
        const problems = validateExecuteFrame(frame, platform)
        if (problems.length > 0) {
            // A frame that fails the contract is never "close enough": the
            // first problem is the one the caller has to fix.
            return this.refusal(frame, 'invalid_request', problems[0])
        }
        if (String(frame.device_id) !== identity.device_id) {
            // The frame names a different install. Its workspace id could still
            // resolve here, so this is checked rather than assumed.
            return this.refusal(frame, 'permission_denied',
                'the frame names a different device')
        }
        // Fetching comes first, verifying second, and the order is the point.
        // The fetch is an *attempt* to obtain the authorized bytes; the verify
        // is the check that they are here and correct. A device that only did
        // the first would run whatever arrived, and one that only did the second
        // would refuse every first run of a new version.
        let declared: DeclaredSkill[]
        try {
            declared = declaredSkills(frame)
        } catch (err) {
            return this.refusal(frame, codeOf(err, 'incompatible_skill'), messageOf(err))
        }
        if (declared.length > 0 && this.options.fetchSkills) {
            try {
                await this.options.fetchSkills(frame, declared)
            } catch (err) {
                return this.refusal(frame, codeOf(err, 'skill_unavailable'), messageOf(err))
            }
        }
        // The skill check comes before the journal, like the frame and device
        // checks above: nothing has run, so there is no start intent to record
        // and no side effect to reconcile. It must come before ``runner.run``
        // because that is what spawns the worker -- a device that ran first and
        // checked afterwards would already have executed the wrong version.
        const skillRefusal = this.verifySkills(frame)
        if (skillRefusal) return skillRefusal
        const result = await this.runner.run(frame, identity)
        return this.resultFrame(frame, result)
    }

    /**
     * Whether the skill versions this frame requires are really here (task 8.9).
     *
     * Returns ``undefined`` when the call may proceed. The four outcomes, and why
     * each is the honest answer rather than a convenience:
     *
     *   * no declared skills -- nothing to prove, this is the ordinary case and
     *     every skill-less tool keeps working exactly as before;
     *   * declared but no cache -- refused, because "I cannot tell which version
     *     I would run" must not be silently read as "no requirements";
     *   * declared and a version is missing -- ``incompatible_skill``, which is
     *     the whole point: a stale device must not run the version it happens to
     *     hold and report success;
     *   * declared and all present -- proceed, and the directories are granted
     *     read-only to the worker (see :meth:`skillRootsFor`).
     */
    verifySkills(frame: Record<string, unknown>): ExecutionResultFrame | undefined {
        let declared: DeclaredSkill[]
        try {
            declared = declaredSkills(frame)
        } catch (err) {
            return this.refusal(frame, codeOf(err, 'incompatible_skill'), messageOf(err))
        }
        if (declared.length === 0) return undefined
        const cache = this.options.skillCache
        if (!cache) {
            return this.refusal(frame, 'skill_cache_unavailable',
                '本机没有技能缓存，无法确认本运行要求的技能版本；已拒绝执行而不是运行未经验证的版本。')
        }
        try {
            cache.resolveDeclared(declared)
            return undefined
        } catch (err) {
            return this.refusal(frame, codeOf(err, 'incompatible_skill'), messageOf(err))
        }
    }

    /**
     * The read-only skill directories this frame's run may use (task 8.9).
     *
     * Empty for a skill-less frame (the ordinary case) and empty when the
     * declared versions are not here -- the refusal in :meth:`verifySkills` is
     * what reports that, so this never has to invent a partial grant. Returning
     * an empty list rather than throwing keeps the caller free of a second copy
     * of the refusal wording.
     */
    skillRootsFor(frame: Record<string, unknown>): string[] {
        const cache = this.options.skillCache
        if (!cache) return []
        try {
            return cache.resolveDeclared(declaredSkills(frame))
        } catch {
            return []
        }
    }

    /** Stop one running command; ``false`` when this device is not running it. */
    async cancel(frame: Record<string, unknown>): Promise<boolean> {
        const identity = this.options.identity(frame)
        if (!identity) return false
        const commandId = String(frame.command_id ?? '')
        if (!commandId) return false
        const scope = {
            origin: identity.origin,
            user_id: identity.user_id,
            tenant_id: identity.tenant_id,
            device_id: identity.device_id,
            command_id: commandId,
        }
        return this.runner.cancel(commandId, scope, String(frame.params_digest ?? ''))
    }

    /**
     * The status the server asks for after a reconnect (task 7.2).
     *
     * Answers from the journal only. A command this device is still running is
     * ``running``; a recorded receipt is its own verdict; anything else is
     * ``queued``, because "not started" is the one answer that is safe to
     * re-drive.
     */
    status(frame: Record<string, unknown>): {
        state: string
        phase?: string
        effects?: string
        result?: unknown
    } {
        const identity = this.options.identity(frame)
        const commandId = String(frame.command_id ?? '')
        if (!identity || !commandId) return { state: 'queued' }
        const scope = {
            origin: identity.origin,
            user_id: identity.user_id,
            tenant_id: identity.tenant_id,
            device_id: identity.device_id,
            command_id: commandId,
        }
        if (this.runner.isRunning(commandId)) return { state: 'running' }
        const found = this.runner.status(commandId, scope)
        if (!found) return { state: 'queued' }
        return {
            state: found.state,
            ...(found.phase ? { phase: found.phase } : {}),
            ...(found.effects ? { effects: found.effects } : {}),
            ...(found.result === undefined ? {} : { result: found.result }),
        }
    }

    /** Mark everything unfinished as ``outcome_unknown`` (startup recovery). */
    recover(): JournalEntry[] {
        return this.runner.recoverUnfinished()
    }

    /** Age out old receipts and enforce the record cap. */
    sweep(options: { retainDays?: number; keepAtMost?: number } = {}): string[] {
        return this.runner.sweepRetention(options)
    }

    /** Stop runs that outlived the connection window (A19). */
    abandonStaleRuns(options: { disconnectedForMs: number }): Promise<JournalEntry[]> {
        return this.runner.abandonStaleRuns(options)
    }

    /** Sign-out or tenant switch: stop this session's work, then stop accepting. */
    revoke(identity: DeviceIdentity): Promise<number> {
        return this.runner.revokeIdentity(identity)
    }

    // -- frame shaping -----------------------------------------------------

    /**
     * The absolute paths one frame touches, for the staleness check (A20).
     *
     * A script's writes are not knowable from its arguments, so ``bash`` gets no
     * prediction rather than a wrong one: a guess here would warn on every
     * legitimate script (or, worse, miss the one that matters).
     */
    private pathsOf(frame: Record<string, unknown>, realRoot: string): FramePaths {
        const tool = String(frame.tool ?? '')
        if (!(tool in TOOL_PATH_ARGS)) return {}
        const args = (frame.arguments && typeof frame.arguments === 'object')
            ? frame.arguments as Record<string, unknown>
            : {}
        const found = TOOL_PATH_ARGS[tool].map((key) => args[key])
            .find((value) => typeof value === 'string' && value !== '')
        if (typeof found !== 'string') return {}
        const absolute = this.absoluteUnder(realRoot, found)
        if (!absolute) return {}
        return tool === 'read' || tool === 'ls' || tool === 'search_files'
            ? { contents: [absolute] }
            : { modifies: [absolute] }
    }

    /** Resolve a project-relative (or in-project absolute) path, or ``null``. */
    private absoluteUnder(realRoot: string, candidate: string): string | null {
        // Late import so this module stays loadable without ``node:path``
        // semantics leaking into the frame-shape tests.
        const path = require('node:path') as typeof import('node:path')
        const resolved = path.isAbsolute(candidate)
            ? path.normalize(candidate)
            : path.resolve(realRoot, candidate)
        const root = path.normalize(realRoot)
        const withSep = root.endsWith(path.sep) ? root : root + path.sep
        // A path outside the project is not "a stale file": it is not this
        // frame's business, and the tool will refuse it on its own terms.
        if (resolved !== root && !resolved.startsWith(withSep)) return null
        return resolved
    }

    /** A terminal frame that reports a refusal before any effect exists. */
    private refusal(
        frame: Record<string, unknown>,
        code: string,
        message: string,
    ): ExecutionResultFrame {
        const now = this.options.now?.() ?? Date.now()
        return {
            type: 'execution_result',
            protocol_major: PROJECT_EXECUTION_PROTOCOL.major,
            command_id: String(frame.command_id ?? ''),
            run_id: String(frame.run_id ?? ''),
            tool_call_id: String(frame.tool_call_id ?? ''),
            state: 'failed',
            execution_phase: 'failed',
            effects: 'none',
            started_at: now,
            finished_at: now,
            error_code: code,
            error_message: message,
        }
    }

    /**
     * Project a runner verdict onto the contract's result frame.
     *
     * ``state`` is what the server persists, and it must be one of the terminal
     * v1 states: ``outcome_unknown`` is ``failed`` with that code, exactly as
     * ``complete_execution`` expects, so "we do not know" is never stored as
     * success.
     */
    private resultFrame(
        frame: Record<string, unknown>,
        result: CommandResult,
    ): ExecutionResultFrame {
        const now = this.options.now?.() ?? Date.now()
        const startedAt = result.entry?.started_at ?? now
        const phase = result.phase
        const state = stateFor(phase)
        const effects = result.effects ?? effectsFor(phase, result.code) ?? 'unknown'
        const payload = result.payload ?? result.entry?.receipt?.payload
            ?? result.entry?.receipt?.frame
        const artifacts = this.artifactsFor(frame, result)
        return {
            type: 'execution_result',
            protocol_major: PROJECT_EXECUTION_PROTOCOL.major,
            command_id: String(frame.command_id ?? ''),
            run_id: String(frame.run_id ?? ''),
            tool_call_id: String(frame.tool_call_id ?? ''),
            state,
            execution_phase: phase as ExecutionResultFrame['execution_phase'],
            effects,
            started_at: startedAt,
            finished_at: result.entry?.completed_at ?? now,
            ...(payload ? { result: payload as ExecutionResultFrame['result'] } : {}),
            ...(artifacts.length ? { artifacts } : {}),
            ...(result.code ? { error_code: result.code } : {}),
            ...(result.message ? { error_message: result.message } : {}),
        }
    }

    /**
     * Files this run really produced, as the device's own references (task 9.1).
     *
     * Built only for a tool whose whole purpose is to write a named file -- the
     * same pair the server publishes cards for -- and only once the run reached
     * a terminal phase. A script's writes are deliberately *not* inferred: a
     * `bash` command's output is not knowable from its arguments, and guessing
     * one (or diffing the whole project) would name files this run did not
     * create. Those files stay reachable through the project's own file panel,
     * which is where a directory listing belongs.
     *
     * A cancelled run may still have written part of its file, so it is included
     * and the file is verified like any other: if it is not there, there is no
     * reference, and the contract's ``cancelled`` effects already say the result
     * may be partial.
     */
    private artifactsFor(frame: Record<string, unknown>,
                         result: CommandResult): ExecutionArtifact[] {
        const phase = result.phase
        if (phase !== 'succeeded' && phase !== 'cancelled') return []
        const tool = String(frame.tool ?? '')
        if (!ARTIFACT_TOOLS.has(tool)) return []
        const workspaceId = String(frame.workspace_id ?? '')
        const realRoot = this.options.realRootOf(workspaceId)
        if (!realRoot) return []
        const candidate = (this.pathsOf(frame, realRoot).modifies ?? [])[0]
        if (!candidate) return []
        const identity = this.options.identity(frame)
        const artifact = buildArtifact({
            realRoot,
            workspaceId,
            deviceId: identity?.device_id ?? String(frame.device_id ?? ''),
            runId: String(frame.run_id ?? ''),
            toolCallId: String(frame.tool_call_id ?? ''),
            candidate,
        })
        return artifact ? [artifact] : []
    }
}

/** Which argument names a path, per tool. ``bash`` deliberately has none. */
const TOOL_PATH_ARGS: Record<string, readonly string[]> = {
    read: CONTENT_ARGS,
    ls: ['path', 'directory', 'dir'],
    search_files: ['path', 'directory', 'dir'],
    write: ['path', 'file_path'],
    edit: ['path', 'file_path'],
}

/**
 * Tools whose success means "this named file now exists" (task 9.1).
 *
 * The same pair master publishes artifact cards for (``_ARTIFACT_TOOLS``), so
 * both modes agree about what counts as a produced output and a user does not
 * see a card in one mode and not the other.
 */
const ARTIFACT_TOOLS = new Set(['write', 'edit'])

/**
 * The v1 state a phase is persisted as.
 *
 * ``cancelling`` is ``running`` with a request attached, and ``outcome_unknown``
 * has no state of its own by design -- it is a failure whose *code* carries the
 * uncertainty, so a row never reads as either success or "nothing happened".
 */
function stateFor(phase: string): string {
    if (phase === 'cancelling') return 'running'
    if (phase === 'outcome_unknown') return 'failed'
    if (isTerminalPhase(phase)) return phase
    return 'running'
}

/** The contract's own ceiling for one call, used by the production adapter. */
export const DEVICE_CALL_TIMEOUT_MS = EXECUTION_LIMITS.script_timeout_max_seconds * 1000
