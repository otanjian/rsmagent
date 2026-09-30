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

import { FsGuard, type GuardChild } from '../local-files/fs-guard'
import type { GrantRegistry } from '../local-files/grants'
import { DeviceClient, type DeviceState } from './device-client'
import { runDeviceCommand, type DeviceCommand, type DeviceResult } from './device-ops'

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
  /** Where the connection state is reported (diagnostics only). */
  onState?: (state: DeviceState, detail?: string) => void
  /**
   * Force the loopback question one way. Only for tests: production decides it
   * from {@link registeredLocalOrigin}.
   */
  allowInsecureLoopback?: boolean
  /** Test seam: replace the client factory. */
  createClient?: (options: ConstructorParameters<typeof DeviceClient>[0]) => DeviceClient
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
      runCommand: (command) => this.runCommand(command),
      onState: this.options.onState,
    })
    this.client.start()
    return true
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
    return runDeviceCommand(guard, fresh.fsGrant, command)
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
