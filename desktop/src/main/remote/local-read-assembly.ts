/**
 * The local read path, assembled: binding -> helper root -> device connection.
 *
 * Change ``fix-desktop-local-context-and-tool-calls`` (task 2.4). Three pieces
 * existed separately and none of them were joined up:
 *
 * * a confirmed binding named a server ``workspace_id``, but nothing on the
 *   machine remembered which *local* root that was;
 * * the file helper could open a root and answer reads, but no command ever
 *   reached it;
 * * the device connection had URL/backoff helpers but no process holding a
 *   socket.
 *
 * This module is the join, and it is deliberately the only place that knows
 * both halves. It keeps:
 *
 * * ``workspace_id -> { local grant, helper root }``, in memory, dropped on
 *   revoke -- a restart starts empty, so a stale candidate can never be used as
 *   an authorization;
 * * one helper process for the app, opened lazily on the first read and closed
 *   with the app;
 * * one device connection, started by the confirmation that made a workspace
 *   real and stopped by detach/logout.
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { FsGuard, type GuardChild } from '../local-files/fs-guard'
import type { GrantRegistry } from '../local-files/grants'
import { buildGrantLaunchPlan } from '../local-execution/launch'
import type { InterpreterProbe, WorkerRuntime } from '../local-execution/interpreter'
import { WorkerSession, DEFAULT_CALL_TIMEOUT_MS, type WorkerReply } from '../local-execution/worker-session'
import { DeviceExecution } from '../project-execution/device-execution'
import { SkillCache, type DeclaredSkill } from '../project-execution/skill-cache'
import { ensureSkillVersions } from '../project-execution/skill-transfer'
import { EXECUTION_LIMITS } from '../project-execution/contract'
import type {
  DeviceIdentity,
  ExecutionOutcome,
  ExecutionRequest,
  CancelHandle,
} from '../project-execution/command-runner'
import type { JournalEntry } from '../project-execution/journal'
import { DeviceClient, type DeviceState } from './device-client'
import {
  runDeviceCommand,
  type DeviceCommand,
  type DeviceResult,
  type MaterializePort,
} from './device-ops'

export interface WorkspaceRoot {
  /** The local grant this workspace came from; re-checked on every command. */
  grantId: string
  /** The helper's own grant for the opened root, once it has been opened. */
  fsGrant: string
  /** Absolute root, main-process only: never serialised, never sent. */
  absolutePath: string
}

export interface LocalReadAssemblyOptions {
  /** The live grant table; a revoked grant must stop resolving. */
  registry: GrantRegistry
  /** How to spawn the helper. Injected so tests can pass a stub. */
  spawnGuard: () => GuardChild
  /**
   * The native bearer for the gateway handshake, read per attempt.
   *
   * Injected rather than imported: this module must stay free of the auth
   * broker so the read path can be driven end to end in a plain Node test.
   */
  token: () => Promise<string | null>
  /**
   * The origin of the *registered* bundled backend, when one is running: the
   * only origin for which an unencrypted ``ws://`` is acceptable.
   */
  registeredLocalOrigin?: () => string
  localGatewayPort?: () => Promise<number>
  /** Where the connection state is reported (diagnostics only). */
  onState?: (state: DeviceState, detail?: string) => void
  /**
   * Force the loopback question one way. Only for tests: production decides it
   * from {@link registeredLocalOrigin}.
   */
  allowInsecureLoopback?: boolean
  /** Test seam: replace the client factory. */
  createClient?: (options: ConstructorParameters<typeof DeviceClient>[0]) => DeviceClient
  /**
   * The v2 execution channel (tasks 7.1 - 7.8).
   *
   * Omitted in a build that does not offer project execution, in which case no
   * ``execute_tool`` frame is answered at all: the server's own capability
   * negotiation decides that, and a client that fabricated a result for a frame
   * it cannot run would be worse than one that stays silent.
   */
  execution?: {
    /** The durable journal, under the app's user-data directory. */
    journalFile: string
    /** The directory the Python package lives in. */
    backendPath: string
    /**
     * The runtime to run the worker with, or ``null`` when none was found.
     *
     * An interpreter in a source checkout, the bundle's own binary in an
     * installed app -- see {@link WorkerRuntime}.
     */
    runtime: WorkerRuntime | null
    /** Probe for the runtime, whose read allowances are derived from it. */
    probe: (runtime: WorkerRuntime) => InterpreterProbe | null
    platform?: NodeJS.Platform
    seatbeltAvailable?: boolean
    baseEnv?: NodeJS.ProcessEnv
    tempBase?: string
    callTimeoutMs?: number
    /**
     * Override the contract's liveness window. Only for tests: production
     * reaps past {@link EXECUTION_LIMITS.run_liveness_seconds}, and a suite that
     * waited that long per case would not be run.
     */
    livenessWindowMs?: number
    /**
     * Where verified skill packages live (task 8.9).
     *
     * Production passes the app's user-data directory and the cache is built
     * here; a test may pass its own {@link SkillCache} instead. ``null`` (or an
     * omitted root) means this build has no skill cache, in which case a frame
     * that *declares* skills is refused rather than run against whatever
     * happens to be installed -- see ``DeviceExecution.verifySkills``.
     */
    skillCache?: SkillCache | null
    skillCacheRoot?: string | null
  }
  /** Where recovery and unsettled runs are surfaced (journal/audit display). */
  onReconcile?: (entry: JournalEntry, reason: string) => void
  /**
   * How this build publishes one local file to the server (task 9.7).
   *
   * Injected, and omitted when the app has no configured origin: a build that
   * cannot reach a server must refuse ``materialize`` by name rather than
   * pretend it delivered something. The helper and its grant are handed in per
   * command, because they belong to the root this command resolved to -- the
   * caller never gets to name a root.
   */
  materialize?: (request: MaterializeCall) => Promise<Record<string, unknown>>
}

/** One explicit upload: the command's request plus the root it resolved against. */
export interface MaterializeCall {
  commandId: string
  workspaceId: string
  relativePath: string
  expectedVersion: string
  guard: FsGuard
  fsGrant: string
}

/**
 * Slack added to the liveness window before a run is reaped.
 *
 * The runner's own comparison is ``disconnectedForMs <= window``, so a timer
 * armed at exactly the window would fire too early and reap nothing.
 */
const LIVENESS_GRACE_MS = 250

/** One root's worker: the sandboxed process that actually runs a tool. */
interface RootWorker {
  session: WorkerSession
  root: string
  tempRoot: string
  /**
   * The read-only skill directories this worker was launched with (task 8.9).
   *
   * The sandbox profile is built once, at ``hello`` time, so a run that pins a
   * different set of skill versions needs a fresh worker rather than inheriting
   * the last run's grants. ``skillKey`` is the comparable form of ``skillRoots``
   * for that decision; ``skillRoots`` is kept so a cancel can report what the
   * tree was allowed to read.
   */
  skillRoots: readonly string[]
  skillKey: string
  /** The worker refuses a concurrent call, so calls queue here per root. */
  tail: Promise<unknown>
}

/**
 * A stable key for one read-only skill set.
 *
 * ``\u0000`` cannot appear in a filesystem path, so two different sets can never
 * collapse into the same key the way a plain join on a printable separator
 * could.
 */
function skillSetKey(skillRoots: readonly string[]): string {
  return [...skillRoots].sort().join('\u0000')
}

/**
 * Owns the helper process + the workspace map + the device connection.
 *
 * Every method is safe to call when nothing is bound: attach/detach and
 * pick/cancel arrive in any order, and "nothing to stop" must never throw.
 */
export class LocalReadAssembly {
  private guard: FsGuard | null = null
  private readonly roots = new Map<string, WorkspaceRoot>()
  private client: DeviceClient | null = null
  private deviceId = ''
  private origin = ''
  private execution: DeviceExecution | null = null
  private readonly workers = new Map<string, RootWorker>()
  /** Which root each running command belongs to, so a cancel finds its tree. */
  private readonly commandRoots = new Map<string, string>()
  /**
   * The sessions this connection actually served, so a sign-out can stop their
   * work. Keyed the way the runner keys an identity, and populated from the
   * grant behind a bound workspace -- never from a frame.
   */
  private readonly sessionIdentities = new Map<string, DeviceIdentity>()
  /** When the connection stopped being able to report a result (task 7.5). */
  private disconnectedSince: number | null = null
  private livenessTimer: ReturnType<typeof setTimeout> | null = null

  constructor(private readonly options: LocalReadAssemblyOptions) {}

  /** Whether a helper process is currently open (diagnostics/tests). */
  get helperOpen(): boolean {
    return !!this.guard && !this.guard.isClosed
  }

  /** The workspaces currently reachable for reads (tests/diagnostics). */
  get boundWorkspaces(): string[] {
    return [...this.roots.keys()]
  }

  /**
   * The live connection facts a locally-issued frame must name (task 9.2).
   *
   * A panel edit is admitted by {@link runExecution}, which re-checks the grant
   * version -- but the *frame* also has to name the connection it was issued
   * under, and only this component knows the epoch the server handed out for
   * the current socket. Reporting both together is what lets the caller refuse
   * an edit while the device is between connections instead of journaling one
   * that can never be delivered.
   */
  connectionSnapshot(): { connected: boolean; epoch: string } {
    const connected = this.client?.currentState === 'ready'
    return {
      connected: !!connected,
      // A stale epoch from a closed socket is worse than none: it would name a
      // connection the server has already fenced, so it is only reported while
      // the connection that owns it is still up.
      epoch: connected ? this.client?.currentEpoch || '' : '',
    }
  }

  /**
   * Remember the workspace a confirmed binding named.
   *
   * Called from the ``bindContext`` confirmation, which is the only path that
   * has both ids: the server's ``workspace_id`` and the local grant the picker
   * activated. The absolute path is read from the registry *now* and never
   * again, so a revoked grant cannot be re-resolved later.
   */
  rememberWorkspace(workspaceId: string, grantId: string): boolean {
    const absolutePath = this.options.registry.absolutePathFor(grantId)
    if (!workspaceId || !absolutePath) return false
    const existing = this.roots.get(workspaceId)
    if (existing && existing.grantId === grantId) return true
    if (existing?.fsGrant) this.guard?.closeRootBestEffort(existing.fsGrant)
    this.roots.set(workspaceId, { grantId, fsGrant: '', absolutePath })
    return true
  }

  /** Drop one workspace (revoke, or a newer binding replaced it). */
  forgetWorkspace(workspaceId: string): void {
    const entry = this.roots.get(workspaceId)
    if (!entry) return
    if (entry.fsGrant) this.guard?.closeRootBestEffort(entry.fsGrant)
    this.roots.delete(workspaceId)
  }

  /** Drop every workspace of a grant (the grant itself was revoked). */
  forgetGrant(grantId: string): void {
    for (const [workspaceId, entry] of [...this.roots.entries()]) {
      if (entry.grantId === grantId) this.forgetWorkspace(workspaceId)
    }
  }

  /** Drop everything: logout, tenant switch, app quit. */
  dispose(): void {
    this.stopClient()
    for (const workspaceId of [...this.roots.keys()]) this.forgetWorkspace(workspaceId)
    this.guard?.dispose()
    this.guard = null
    this.deviceId = ''
    this.origin = ''
    this.clearLivenessTimer()
    // Terminating this session's work is what "signed out" has to mean, so it
    // happens here rather than being left to the next frame (task 7.5). The
    // journal keeps its partial-effects claim either way.
    void this.revokeSessions()
    this.commandRoots.clear()
  }

  /**
   * Stop every session's running work, then every worker (task 7.5).
   *
   * Awaited by nobody in production -- ``dispose`` cannot block the app's
   * shutdown path -- but the termination itself is awaited *inside* the runner,
   * so a stopped script's receipt is written by the path that owns it before
   * its tree is torn down here.
   */
  private async revokeSessions(): Promise<void> {
    const endpoint = this.execution
    const identities = [...this.sessionIdentities.values()]
    this.sessionIdentities.clear()
    this.execution = null
    if (endpoint) {
      for (const identity of identities) {
        try {
          await endpoint.revoke(identity)
        } catch {
          /* the receipts already written stand; a failed stop is reported */
        }
      }
    }
    await this.stopWorkers()
  }

  /**
   * Stop every sandboxed worker and its trees.
   *
   * Awaited by {@link revokeSessions}, because "signed out" must mean the
   * scripts are gone, not "their receipts were written".
   */
  private async stopWorkers(): Promise<void> {
    const workers = [...this.workers.values()]
    this.workers.clear()
    for (const worker of workers) {
      try {
        await worker.session.stop()
      } catch {
        /* the worker may already be gone */
      }
      try {
        fs.rmSync(worker.tempRoot, { recursive: true, force: true })
      } catch {
        /* best effort: a temp dir left behind is reported, not fatal */
      }
    }
  }

  /**
   * Make this install reachable for the workspace that was just confirmed.
   *
   * Starting the connection *here* -- rather than at app start -- is what keeps
   * "the device is connected" and "a directory is bound" the same fact: a
   * connection with no binding could only ever be handed commands it must
   * refuse.
   */
  bindDevice(options: { origin: string; deviceId: string; workspaceId: string; grantId: string }): boolean {
    // Both facts are prerequisites, not side effects: without a device id the
    // hello could never be sent, and recording the workspace anyway would leave
    // a mapping nothing can serve.
    const origin = (options.origin || '').replace(/\/+$/, '')
    if (!origin || !options.deviceId) return false
    if (!this.rememberWorkspace(options.workspaceId, options.grantId)) return false
    if (this.client && this.origin === origin && this.deviceId === options.deviceId) {
      return true
    }
    this.stopClient()
    this.origin = origin
    this.deviceId = options.deviceId
    const create = this.options.createClient ?? ((opts) => new DeviceClient(opts))
    this.client = create({
      origin,
      deviceId: async () => this.deviceId || null,
      token: () => this.options.token(),
      allowInsecureLoopback: this.loopbackAllowed(origin),
      localGatewayPort: this.loopbackAllowed(origin) ? this.options.localGatewayPort : undefined,
      runCommand: (command) => this.runCommand(command),
      runExecution: (frame) => this.runExecution(frame),
      runExecutionStatus: (frame) => this.runExecutionStatus(frame),
      onState: (state, detail) => this.noteState(state, detail),
    })
    this.client.start()
    return true
  }

  /**
   * The connection supervisor's own view of reachability (task 7.5).
   *
   * A short outage is not a licence to keep running or to accept new work: the
   * runner refuses new frames the moment ``connected()`` goes false, and a run
   * that outlives the contract's liveness window is stopped here, because by
   * then there is nobody left to report it to. The session's own state at the
   * top of a frame is what the server re-drives, so "still reconnecting" is the
   * only state this timer has to cover.
   */
  private noteState(state: DeviceState, detail?: string): void {
    this.options.onState?.(state, detail)
    if (state === 'ready') {
      this.disconnectedSince = null
      this.clearLivenessTimer()
      return
    }
    if (state === 'stopped') return
    // ``connecting``, ``reconnecting`` and ``idle`` all mean "this device cannot
    // report a result right now", which is the only fact the timer needs.
    if (this.disconnectedSince === null) this.disconnectedSince = Date.now()
    this.armLivenessTimer()
  }

  private armLivenessTimer(): void {
    if (this.livenessTimer) return
    const window = this.options.execution?.livenessWindowMs
      ?? EXECUTION_LIMITS.run_liveness_seconds * 1000
    // A margin, because the runner's own comparison is ``<= window``: firing at
    // exactly the window boundary would be a no-op and the run would never be
    // reaped.
    const armed = setTimeout(() => {
      this.livenessTimer = null
      void this.reapStaleRuns()
    }, window + LIVENESS_GRACE_MS)
    // Never a reason to keep the process alive.
    ;(armed as { unref?: () => void }).unref?.()
    this.livenessTimer = armed
  }

  private clearLivenessTimer(): void {
    if (!this.livenessTimer) return
    clearTimeout(this.livenessTimer)
    this.livenessTimer = null
  }

  private async reapStaleRuns(): Promise<void> {
    const endpoint = this.execution
    const since = this.disconnectedSince
    if (!endpoint || since === null) return
    try {
      await endpoint.abandonStaleRuns({ disconnectedForMs: Date.now() - since })
    } catch {
      /* a timer must not throw; the journal keeps whatever it recorded */
    }
  }

  /**
   * Whether plain ``ws://`` is acceptable for this origin.
   *
   * Only for the *registered* bundled backend on loopback, which is the same
   * exception the probe and the container attach already use. A stored remote
   * profile is never allowed to downgrade.
   */
  private loopbackAllowed(origin: string): boolean {
    if (this.options.allowInsecureLoopback === false) return false
    const registered = (this.options.registeredLocalOrigin?.() || '').replace(/\/+$/, '')
    if (!registered || registered !== origin) return false
    try {
      const url = new URL(origin)
      const host = (url.hostname || '').toLowerCase()
      return url.protocol === 'http:' && (host === '127.0.0.1' || host === 'localhost' || host === '::1')
    } catch {
      return false
    }
  }

  private stopClient(): void {
    this.client?.stop()
    this.client = null
  }

  /**
   * Execute one command against the workspace the server named.
   *
   * The workspace map is the authorization on this side: a command for a
   * workspace that is not bound here (revoked, replaced, or never confirmed in
   * this process) fails as ``stale_context`` instead of guessing a root.
   */
  async runCommand(command: DeviceCommand): Promise<DeviceResult> {
    const entry = this.roots.get(command.workspace_id)
    if (!entry) {
      return {
        state: 'failed',
        errorCode: 'stale_context',
        errorMessage: 'the local directory is no longer bound to this session',
      }
    }
    // A grant can be revoked while a command is in flight; re-resolving through
    // the registry is what makes that take effect on the next command.
    if (!this.options.registry.absolutePathFor(entry.grantId)) {
      this.forgetWorkspace(command.workspace_id)
      return {
        state: 'failed',
        errorCode: 'stale_context',
        errorMessage: 'the local directory grant was revoked',
      }
    }
    const guard = this.ensureGuard()
    if (!guard) {
      return {
        state: 'failed',
        errorCode: 'feature_unavailable',
        errorMessage: 'the local file helper is not available',
      }
    }
    if (!entry.fsGrant) {
      try {
        const opened = await guard.openRoot(entry.absolutePath)
        if (!opened.grant) {
          return {
            state: 'failed',
            errorCode: 'io_error',
            errorMessage: 'the local directory could not be opened',
          }
        }
        // The old root for a re-picked directory is released before the new one
        // is recorded, so no descriptor outlives its grant.
        const current = this.roots.get(command.workspace_id)
        if (!current || current.grantId !== entry.grantId) {
          guard.closeRootBestEffort(opened.grant)
          return {
            state: 'failed',
            errorCode: 'stale_context',
            errorMessage: 'the local directory changed while it was being opened',
          }
        }
        current.fsGrant = opened.grant
      } catch (err) {
        return {
          state: 'failed',
          errorCode: (err as { code?: string })?.code || 'io_error',
          errorMessage: (err as Error)?.message || 'the local directory could not be opened',
        }
      }
    }
    const fresh = this.roots.get(command.workspace_id)
    if (!fresh?.fsGrant) {
      return {
        state: 'failed',
        errorCode: 'stale_context',
        errorMessage: 'the local directory is no longer bound to this session',
      }
    }
    // ``materialize`` is the one op that needs the app's upload transport, which
    // is bound here (per command) to the guard and grant of the root the server
    // named -- so the port can never be asked about a root this process did not
    // just confirm.
    const deliver = this.options.materialize
    const materialize: MaterializePort | undefined = deliver
      ? (request) => deliver({
        ...request,
        guard,
        fsGrant: fresh.fsGrant as string,
      })
      : undefined
    return runDeviceCommand(guard, fresh.fsGrant, command, { materialize })
  }

  // -- v2 execution (change ``align-...``, tasks 7.1 - 7.8) ---------------

  /**
   * The skill cache for this build, or ``null`` when it was not configured.
   *
   * An explicit ``skillCache`` wins so a test can point at its own directory.
   * Otherwise the cache is built from ``skillCacheRoot``, which production sets
   * to the app's user-data directory. No root and no cache means ``null``, and
   * a frame that declares skills is then refused -- the honest answer, because
   * "I was not told where skill packages live" is not evidence that none are
   * required.
   */
  private skillCacheFor(config: NonNullable<LocalReadAssemblyOptions['execution']>): SkillCache | null {
    if (config.skillCache !== undefined) return config.skillCache
    const root = config.skillCacheRoot
    return root ? new SkillCache(root) : null
  }

  /**
   * The device's execution endpoint, built once per process.
   *
   * Housekeeping runs at construction, and this is deliberately the process's
   * only such seam: a start intent with no receipt has to be surfaced *before*
   * the first frame arrives, because the answer to "may this command run"
   * depends on it, and the retention sweep belongs beside it for the same
   * reason -- a journal that is never swept grows without bound on a real
   * install, where nobody is looking.
   *
   * The sweep uses the contract's own ``journal_retain_days`` and nothing else.
   * There is no record cap here on purpose: the contract defines no such number
   * for the journal, and inventing one in the client would put a bound in the
   * code that no reviewer could check against the contract. ``prune`` still
   * accepts a cap for callers that have a real reason to pass one.
   */
  private executionEndpoint(): DeviceExecution | null {
    const config = this.options.execution
    if (!config) return null
    if (this.execution) return this.execution
    const endpoint = new DeviceExecution({
      identity: (frame) => this.identityFor(frame),
      realRootOf: (workspaceId) => this.executableRootFor(workspaceId),
      runTool: (request) => this.runDeviceTool(request),
      terminate: (handle) => this.stopDeviceRun(handle),
      fetchSkills: (frame, declared) => this.fetchSkillVersions(frame, declared),
      journalFile: config.journalFile,
      // Built once from the root, so the frame check and the worker grant below
      // read the same cache: two instances over one directory would be two
      // answers to "is this version installed".
      ...(this.skillCacheFor(config) ? { skillCache: this.skillCacheFor(config) } : {}),
      ...(config.platform ? { platform: config.platform } : {}),
      connected: () => this.client?.currentState === 'ready',
      ...(this.options.onReconcile
        ? { onReconcile: this.options.onReconcile } : {}),
    })
    // Never retried: the journal's whole point is that a human decides.
    endpoint.recover()
    // After recovery, never before: recovery gives an unfinished intent a
    // completion timestamp, and sweeping first would be judging records whose
    // state recovery had not yet settled. ``prune`` skips anything that is not
    // ``completed``, so the unknowns recovery just wrote are never reclaimed --
    // they are the only evidence that the command might have had an effect.
    endpoint.sweep()
    this.execution = endpoint
    return endpoint
  }

  /**
   * Obtain the skill versions a frame declares, over the native channel (8.9).
   *
   * The device asks the *server it is connected to* -- ``this.origin``, which
   * came from this device's own registration, never from the frame -- for the
   * exact ``(skill_id, digest)`` pairs the frame names, and installs each one
   * only after the cache recomputes its digest. So the fetch cannot widen what
   * the run may use: the version that ends up mounted is the one the frame
   * declared, or nothing.
   *
   * The command identifiers travel with the request so the server can check the
   * version against the set *that command* was authorized with. They are read
   * from the frame because this is the frame being served, and the server
   * treats them as a claim to verify, not a fact to trust.
   *
   * A refusal is thrown, not returned: ``DeviceExecution.execute`` turns it into
   * a result frame carrying this error's own code, so the server and the model
   * see the same reason the device refused.
   */
  private async fetchSkillVersions(
    frame: Record<string, unknown>,
    declared: readonly DeclaredSkill[],
  ): Promise<void> {
    const cache = this.skillCacheFor(this.options.execution!)
    if (!cache) {
      throw Object.assign(
        new Error('本机没有技能缓存，无法接收本运行要求的技能包。'),
        { code: 'skill_cache_unavailable' })
    }
    if (!this.origin) {
      throw Object.assign(
        new Error('本机尚未连接服务器，无法获取技能包。'),
        { code: 'device_offline' })
    }
    const bearer = await this.options.token()
    if (!bearer) {
      throw Object.assign(
        new Error('本机没有可用的原生会话，无法获取技能包。'),
        { code: 'permission_denied' })
    }
    // The frame's own envelope fields: the digest is what ties this pull to the
    // command the server authorized, and the rest identify the row.
    const result = await ensureSkillVersions({
      cache,
      declared,
      origin: this.origin,
      nativeBearer: bearer,
      commandId: String(frame.command_id ?? ''),
      deviceId: String(frame.device_id ?? ''),
      bindingId: String(frame.binding_id ?? ''),
      workspaceId: String(frame.workspace_id ?? ''),
      grantVersion: Number(frame.grant_version ?? 0),
      paramsDigest: String(frame.params_digest ?? ''),
    })
    if (!result.ok) {
      throw Object.assign(new Error(result.message), { code: result.code })
    }
  }

  /** The device's own session for the frame's workspace, or ``null``. */
  private identityFor(frame: Record<string, unknown>): DeviceIdentity | null {    const entry = this.roots.get(String(frame.workspace_id || ''))
    const grant = entry ? this.liveGrant(entry.grantId) : null
    if (!grant) return null
    // The origin is the *connection's*: a frame cannot name where this device
    // thinks it is signed in.
    const identity: DeviceIdentity = {
      origin: this.origin,
      user_id: grant.userId,
      tenant_id: grant.tenantId,
      device_id: grant.deviceId,
    }
    // Remembered so a sign-out stops *this* session's work even after the
    // grant table has been cleared (task 7.5).
    this.sessionIdentities.set(
      `${identity.user_id}\u0000${identity.tenant_id}`, identity)
    return identity
  }

  /** The live grant behind a bound workspace, re-read on every frame. */
  private liveGrant(grantId: string) {
    return this.options.registry.list().find((grant) => grant.id === grantId) ?? null
  }

  /**
   * The real root a workspace may execute in, or ``null``.
   *
   * Three things must hold at this moment, not at bind time: the workspace is
   * still bound here, its grant is still live, and that grant is a
   * ``project-execution`` grant. A read-only grant is *not* widened into
   * execution by being present.
   */
  private executableRootFor(workspaceId: string): string | null {
    const entry = this.roots.get(workspaceId)
    if (!entry) return null
    const grant = this.liveGrant(entry.grantId)
    if (!grant || grant.purpose !== 'project-execution') return null
    return this.options.registry.absolutePathFor(entry.grantId)
  }

  /**
   * Run one admitted v2 frame, after the version match is re-checked.
   *
   * The gate and the journal live in {@link DeviceExecution}; what is checked
   * here is the one thing it cannot see -- that the frame's ``grant_version`` is
   * the version that is live now. A re-picked directory bumps the version, so a
   * frame issued before the repick is a miss rather than a write into a
   * directory the user no longer authorized.
   */
  async runExecution(frame: Record<string, unknown>): Promise<Record<string, unknown>> {
    const endpoint = this.executionEndpoint()
    if (!endpoint) {
      return this.executionRefusal(frame, 'runtime_unavailable',
        'this build does not offer project execution')
    }
    const workspaceId = String(frame.workspace_id || '')
    const entry = this.roots.get(workspaceId)
    const grant = entry ? this.liveGrant(entry.grantId) : null
    // A re-picked directory bumps the version, so a frame issued before the
    // repick is a miss rather than a write into a directory the user no longer
    // authorized.
    if (!grant || grant.purpose !== 'project-execution'
      || grant.grantVersion !== Number(frame.grant_version || 0)) {
      return this.executionRefusal(frame, 'grant_revoked',
        'the frame names a grant version that is no longer live on this device')
    }
    return endpoint.execute(frame) as unknown as Record<string, unknown>
  }

  /** A terminal v2 frame for a refusal taken before the runner was reached. */
  private executionRefusal(
    frame: Record<string, unknown>,
    code: string,
    message: string,
  ): Record<string, unknown> {
    return {
      type: 'execution_result',
      protocol_major: 2,
      command_id: String(frame.command_id || ''),
      run_id: String(frame.run_id || ''),
      tool_call_id: String(frame.tool_call_id || ''),
      state: 'failed',
      execution_phase: 'failed',
      // Nothing was run here, so this device claims no effect.
      effects: 'none',
      started_at: 0,
      finished_at: 0,
      error_code: code,
      error_message: message,
    }
  }

  /** Answer a post-reconnect status query from the durable journal. */
  runExecutionStatus(frame: Record<string, unknown>): Record<string, unknown> {
    const endpoint = this.executionEndpoint()
    if (!endpoint) return { state: 'queued' }
    return endpoint.status(frame) as unknown as Record<string, unknown>
  }

  /**
   * Run one tool inside the sandbox for the root the frame resolved to.
   *
   * Calls are serialised per root because the worker refuses a concurrent call;
   * the gate already runs effectful commands alone, so this queue only ever
   * holds read-only calls, and the contract's parallelism is a ceiling rather
   * than a promise that reads must overlap.
   */
  private async runDeviceTool(request: ExecutionRequest): Promise<ExecutionOutcome> {
    const config = this.options.execution
    if (!config) {
      return {
        exit_code: -1,
        error_code: 'runtime_unavailable',
        error_message: 'project execution is not configured on this device',
      }
    }
    // Resolved from the frame through the *same* cache the admission check used
    // (task 8.9), so the directories granted here are the versions that were
    // verified -- not a second lookup that could drift from it. Empty for a
    // skill-less frame, which is the ordinary case.
    const skillRoots = this.executionEndpoint()?.skillRootsFor(request.frame) ?? []
    const worker = await this.workerFor(request.realRoot, skillRoots)
    if ('refused' in worker) return worker.refused
    const tool = String(request.frame.tool || '')
    const args = (request.frame.arguments && typeof request.frame.arguments === 'object')
      ? request.frame.arguments as Record<string, unknown>
      : {}
    const run = worker.tail.then(
      () => worker.session.call(tool, args, this.callBudgetMs(request.frame)))
    // Keep the chain alive after a failure so one bad call does not wedge the root.
    worker.tail = run.catch(() => undefined)
    this.commandRoots.set(request.handle.commandId, request.realRoot)
    try {
      return outcomeFromReply(await run)
    } finally {
      this.commandRoots.delete(request.handle.commandId)
    }
  }

  /**
   * Stop one run's process tree, resolving only once it has ended.
   *
   * ``interruptAndReap`` cancels the worker's current call and then kills the
   * tree, including the groups the sandboxed worker was not permitted to signal
   * itself -- which is exactly why a cancel must be awaited rather than fired.
   */
  private async stopDeviceRun(handle: CancelHandle): Promise<void> {
    const worker = this.workers.get(handle.realRoot)
    if (!worker) return
    await worker.session.interruptAndReap().catch(() => undefined)
  }

  private async workerFor(
    root: string,
    skillRoots: readonly string[] = [],
  ): Promise<RootWorker | { refused: ExecutionOutcome }> {
    const skillKey = skillSetKey(skillRoots)
    const existing = this.workers.get(root)
    if (existing && existing.session.running && existing.skillKey === skillKey) return existing
    if (existing) {
      // A different read-only set (or a dead worker) means the previous process
      // may not serve this run: its sandbox profile already allowed a different
      // set of skill versions, and reusing it would grant whichever set was
      // wider. Tearing it down before the new one starts also keeps the number
      // of live workers bounded by the number of roots.
      this.workers.delete(root)
      await existing.session.stop().catch(() => undefined)
      fs.rmSync(existing.tempRoot, { recursive: true, force: true })
    }
    const config = this.options.execution
    if (!config) {
      return { refused: {
        exit_code: -1,
        error_code: 'runtime_unavailable',
        error_message: 'project execution is not configured on this device',
      } }
    }
    let tempRoot: string
    try {
      tempRoot = fs.mkdtempSync(path.join(config.tempBase ?? os.tmpdir(), 'cow-device-run-'))
      fs.chmodSync(tempRoot, 0o700)
    } catch (err) {
      return { refused: {
        exit_code: -1,
        error_code: 'resource_unavailable',
        error_message: (err as Error)?.message || 'the run directory could not be created',
      } }
    }
    const plan = buildGrantLaunchPlan({
      platform: config.platform ?? process.platform,
      grant: { projectRoot: root, tempRoot, skillRoots },
      interpreter: config.runtime ? config.probe(config.runtime) : null,
      seatbeltAvailable: config.seatbeltAvailable,
      backendPath: config.backendPath,
      baseEnv: config.baseEnv ?? process.env,
    })
    if (!plan.ok) {
      fs.rmSync(tempRoot, { recursive: true, force: true })
      // The launcher's own reason, not a translated one: "no sandbox on this
      // platform" and "no interpreter" need different fixes.
      return { refused: {
        exit_code: -1,
        error_code: plan.code,
        error_message: plan.reason,
      } }
    }
    const session = new WorkerSession(
      plan,
      { onStderr: (chunk) => process.stderr.write(`[device-exec] ${chunk}`) },
      { callTimeoutMs: config.callTimeoutMs ?? DEFAULT_CALL_TIMEOUT_MS },
    )
    const hello = await session.hello(root, tempRoot)
    if (hello.status !== 'success') {
      await session.stop().catch(() => undefined)
      fs.rmSync(tempRoot, { recursive: true, force: true })
      return { refused: {
        exit_code: -1,
        error_code: 'runtime_unavailable',
        error_message: hello.error?.message || 'the local worker did not start',
      } }
    }
    const worker: RootWorker = {
      session, root, tempRoot, skillRoots, skillKey, tail: Promise.resolve(),
    }
    this.workers.set(root, worker)
    return worker
  }

  /** The frame's own budget, bounded by the contract's ceiling. */
  private callBudgetMs(frame: Record<string, unknown>): number {
    const seconds = Number(frame.deadline_seconds ?? 0)
    const max = DEFAULT_CALL_TIMEOUT_MS
    if (!Number.isFinite(seconds) || seconds <= 0) return max
    return Math.min(Math.round(seconds * 1000), max)
  }

  private ensureGuard(): FsGuard | null {
    if (this.guard && !this.guard.isClosed) return this.guard
    try {
      this.guard = new FsGuard(this.options.spawnGuard)
    } catch {
      this.guard = null
    }
    return this.guard
  }
}

/**
 * The worker's reply, as the runner's own outcome vocabulary.
 *
 * A worker error is *not* a transport failure here: the tool ran (or refused)
 * and the model is entitled to its reason. ``worker_exited`` and
 * ``launch_failed`` are the two that mean the runtime broke rather than the tool
 * answering, and those are reported as ``runtime_unavailable`` -- an unknown
 * outcome, never a quiet success.
 */
function outcomeFromReply(reply: WorkerReply): ExecutionOutcome {
  const duration = typeof reply.duration_ms === 'number' ? reply.duration_ms : undefined
  const payload = {
    status: reply.status === 'success' ? 'success' : 'error',
    result: reply.result ?? null,
    display: reply.display ?? null,
    ext_data: reply.ext_data ?? null,
    ...(duration === undefined ? {} : { duration_ms: duration }),
  }
  if (reply.error) {
    const code = reply.error.code || 'tool_failed'
    if (code === 'worker_exited' || code === 'launch_failed') {
      return {
        exit_code: -1,
        error_code: 'runtime_unavailable',
        error_message: reply.error.message,
        payload,
      }
    }
    return {
      exit_code: -1,
      error_code: code === 'timeout' ? 'deadline_exceeded' : code,
      error_message: reply.error.message,
      ...(duration === undefined ? {} : { duration_ms: duration }),
      payload,
    }
  }
  return {
    exit_code: 0,
    ...(typeof reply.result === 'string' ? { stdout: reply.result } : {}),
    ...(duration === undefined ? {} : { duration_ms: duration }),
    payload,
  }
}
