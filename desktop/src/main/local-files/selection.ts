/**
 * The trusted native directory-selection service (tasks 2.3 / 2.4).
 *
 * Both entry points -- the Web adapter's ``chooseWorkspace`` and a local one --
 * ask *this* for a selection, and it is the only thing that talks to the native
 * picker. What it deliberately is not: a filesystem or execution IPC. It returns
 * a purpose-limited record (``GrantPublic``), never a path to the caller, and the
 * path never becomes reachable by asking it a different question.
 *
 * Three properties it exists to provide, each of which the naive
 * "await dialog, then activate" version of this flow does not have:
 *
 * 1. **Generation.** A dialog is modal and slow, so two selections can overlap:
 *    the user re-picks while the first dialog is still closing, or a page fires
 *    a second request because it thinks the first failed. An older attempt that
 *    resolves *after* a newer one started must be discarded -- its Promise
 *    callback still runs, and if it committed it would activate a root the user
 *    has already moved past. Every attempt records its generation and refuses to
 *    commit unless it is still the newest (`superseded`).
 * 2. **Cancel keeps the previous authorization.** Cancelling is not a failure and
 *    must not clear what is already in effect: the caller is told the selection
 *    is cancelled *and* which selection still applies, so the UI cannot imply the
 *    user now has no project.
 * 3. **A phase the UI can show honestly.** ``idle → selecting → validating →
 *    preparing → ready``, with ``failed`` carrying a code. A failure leaves the
 *    previous selection in effect for the same reason cancel does.
 *
 * Pure Node, with the native parts injected, so all of the above is tested
 * without opening a window (`tests/test_desktop_local_selection.cjs`).
 */

import { promises as fs } from 'node:fs'

import type { GrantPublic, GrantPurpose, GrantScope } from './grants'

export type SelectionPhase =
  | 'idle'
  | 'selecting'
  | 'validating'
  | 'preparing'
  | 'ready'
  | 'failed'

export interface SelectionError {
  code: string
  message: string
}

export interface SelectionState {
  phase: SelectionPhase
  /**
   * Attempt counter for this scope. Bumped for *every* attempt, including ones
   * that are cancelled or superseded, so a caller can tell two attempts apart.
   */
  generation: number
  /** The selection actually in effect (the last committed one), or null. */
  effective: GrantPublic | null
  /** The last failure, when ``phase`` is ``failed``. */
  error: SelectionError | null
  /**
   * What the user was asked to consent to, once a dialog was shown. Recorded
   * because "which authorization did the user actually agree to" is not
   * recoverable from the grant alone when the wording changed.
   */
  message?: string
}

export interface SelectionDialogResult {
  canceled?: boolean
  filePaths?: string[]
}

export interface PrepareResult {
  ok: boolean
  code?: string
  message?: string
}

export interface SelectionRequest {
  scope: GrantScope
  purpose: GrantPurpose
  /** Shows the native picker. The only part that needs Electron. */
  pick: (request: { message: string; purpose: GrantPurpose }) => Promise<SelectionDialogResult | null>
  /** Commits a validated root. Implemented by the caller that owns persistence. */
  commit: (root: { absolutePath: string; label: string; purpose: GrantPurpose }) => GrantPublic
  /**
   * Bring the execution environment up (launcher, worker, cache). Optional: a
   * caller with nothing to prepare must say so rather than fake a phase, and the
   * state machine then goes straight from validating to ready.
   */
  prepare?: (grant: GrantPublic) => Promise<PrepareResult>
  /**
   * Undo a `commit` whose attempt was discarded (preparation failed, or a newer
   * attempt overtook it). Optional but recommended: without it the service cannot
   * un-activate the grant, and says so by reporting it as still effective rather
   * than pretending the previous selection is back.
   */
  release?: (grant: GrantPublic) => void
}

export type SelectionOutcome =
  | { ok: true; committed: true; state: SelectionState }
  | { ok: true; committed: false; reason: 'cancelled' | 'superseded'; state: SelectionState }
  | { ok: false; code: string; message: string; state: SelectionState }

export interface SelectionServiceOptions {
  /** The message shown for a purpose. Injected so tests pin the wording. */
  messageFor: (purpose: GrantPurpose) => string
  /**
   * Whether the picked root is usable. Injected; the default checks that it is
   * a directory the process can read, which is what "validating" means.
   */
  validate?: (absolutePath: string) => Promise<{ ok: true } | { ok: false; code: string; message: string }>
}

/** The production validator: the picker's word is not the check. */
export async function validateDirectory(
  absolutePath: string,
): Promise<{ ok: true } | { ok: false; code: string; message: string }> {
  try {
    const info = await fs.stat(absolutePath)
    if (!info.isDirectory()) {
      return { ok: false, code: 'not_a_directory', message: 'the selection is not a directory' }
    }
  } catch {
    return { ok: false, code: 'not_a_directory', message: 'the selection cannot be read' }
  }
  try {
    await fs.access(absolutePath, fs.constants.R_OK | fs.constants.X_OK)
  } catch {
    return { ok: false, code: 'not_readable', message: 'the selection is not readable' }
  }
  return { ok: true }
}

function scopeKey(scope: GrantScope): string {
  return [scope.serverId, scope.userId, scope.tenantId, scope.deviceId].join('\u0001')
}

function labelOf(absolutePath: string): string {
  const trimmed = absolutePath.replace(/[\\/]+$/, '')
  const parts = trimmed.split(/[\\/]/)
  return parts[parts.length - 1] || trimmed
}

/**
 * The selection state machine, one entry per scope.
 *
 * Per scope rather than global: the tenant / account / device a selection belongs
 * to is part of the selection, and a state shared across scopes would let one
 * scope's "ready" be read as another's.
 */
export class DirectorySelectionService {
  private readonly states = new Map<string, SelectionState>()
  private readonly generations = new Map<string, number>()
  private readonly messageFor: (purpose: GrantPurpose) => string
  private readonly validate: (
    absolutePath: string,
  ) => Promise<{ ok: true } | { ok: false; code: string; message: string }>

  constructor(options: SelectionServiceOptions) {
    this.messageFor = options.messageFor
    this.validate = options.validate ?? validateDirectory
  }

  /** The state for a scope. Path-free by construction. */
  state(scope: GrantScope): SelectionState {
    return { ...(this.states.get(scopeKey(scope)) ?? EMPTY_STATE) }
  }

  /**
   * Forget a scope entirely: its state and its effective selection.
   *
   * The scope-change / logout path. A grant dropped elsewhere (revoke, tenant
   * switch) must not leave a stale "ready" claiming a root is still in effect.
   */
  forget(scope: Partial<GrantScope>): void {
    for (const key of [...this.states.keys()]) {
      if (matchesScope(key, scope)) {
        this.states.delete(key)
        this.generations.delete(key)
      }
    }
  }

  /**
   * Mark the effective selection gone without a new attempt (an explicit revoke),
   * keeping the generation so an in-flight attempt is still superseded correctly.
   */
  markRevoked(scope: GrantScope): void {
    const key = scopeKey(scope)
    const state = this.states.get(key)
    if (!state) return
    this.states.set(key, {
      ...state,
      phase: state.phase === 'ready' ? 'idle' : state.phase,
      effective: null,
    })
  }

  /** Record a committed selection the caller activated elsewhere (e.g. restore). */
  markCommitted(scope: GrantScope, grant: GrantPublic): void {
    const key = scopeKey(scope)
    const generation = this.generations.get(key) ?? 0
    this.states.set(key, {
      phase: 'ready',
      generation,
      effective: grant,
      error: null,
    })
  }

  /**
   * Run one selection attempt to completion.
   *
   * Never throws: a caller here is a bridge method answering a page, and a
   * rejection would surface as an opaque bridge error instead of a state the UI
   * can render.
   */
  async select(request: SelectionRequest): Promise<SelectionOutcome> {
    const key = scopeKey(request.scope)
    const generation = (this.generations.get(key) ?? 0) + 1
    this.generations.set(key, generation)
    const previous = this.states.get(key) ?? EMPTY_STATE
    const message = this.messageFor(request.purpose)
    this.states.set(key, {
      phase: 'selecting',
      generation,
      effective: previous.effective,
      error: null,
      message,
    })

    const abandoned = (): boolean => this.generations.get(key) !== generation

    let dialogResult: SelectionDialogResult | null
    try {
      dialogResult = await request.pick({ message, purpose: request.purpose })
    } catch (err: any) {
      return this.fail(key, generation, {
        code: 'picker_failed',
        message: String((err && err.message) || err),
      })
    }
    // Checked before anything is committed: a newer attempt owns the state now,
    // and this one must not even write to it.
    if (abandoned()) {
      return { ok: true, committed: false, reason: 'superseded', state: this.state(request.scope) }
    }

    const absolutePath = pickPath(dialogResult)
    if (!absolutePath) {
      // Cancel is not a failure, and it does not clear what is in effect.
      this.states.set(key, {
        ...previous,
        generation,
        message,
      })
      return { ok: true, committed: false, reason: 'cancelled', state: this.state(request.scope) }
    }

    this.states.set(key, {
      phase: 'validating',
      generation,
      effective: previous.effective,
      error: null,
      message,
    })
    const validation = await this.validate(absolutePath)
    if (abandoned()) {
      return { ok: true, committed: false, reason: 'superseded', state: this.state(request.scope) }
    }
    if (!validation.ok) {
      return this.fail(key, generation, { code: validation.code, message: validation.message })
    }

    let grant: GrantPublic
    try {
      grant = request.commit({
        absolutePath,
        label: labelOf(absolutePath),
        purpose: request.purpose,
      })
    } catch (err: any) {
      return this.fail(key, generation, {
        code: 'invalid_request',
        message: String((err && err.message) || err),
      })
    }

    if (request.prepare) {
      this.states.set(key, {
        phase: 'preparing',
        generation,
        effective: previous.effective,
        error: null,
        message,
      })
      let prepared: PrepareResult
      try {
        prepared = await request.prepare(grant)
      } catch (err: any) {
        return this.discard(request, grant, {
          code: 'prepare_failed',
          message: String((err && err.message) || err),
        })
      }
      // Re-checked after the async step: the dialog for a newer attempt may have
      // been opened while this one was preparing.
      if (abandoned()) {
        request.release?.(grant)
        return { ok: true, committed: false, reason: 'superseded', state: this.state(request.scope) }
      }
      if (!prepared.ok) {
        return this.discard(request, grant, {
          code: prepared.code || 'prepare_failed',
          message: prepared.message || 'the execution environment could not be prepared',
        })
      }
    }

    this.states.set(key, {
      phase: 'ready',
      generation,
      effective: grant,
      error: null,
      message,
    })
    return { ok: true, committed: true, state: this.state(request.scope) }
  }

  /**
   * Report a failure for a root that was already activated.
   *
   * `commit` runs before `prepare` because preparation needs the grant, which
   * means a failed preparation leaves a live grant in the registry. Reported
   * honestly: the grant is released when the caller gave a way to release it, and
   * otherwise it is shown as still in effect, because it is. Claiming the previous
   * selection is back while the registry holds the new one is the one outcome that
   * would let the UI and the authorization disagree.
   */
  private discard(request: SelectionRequest, grant: GrantPublic, error: SelectionError): SelectionOutcome {
    const key = scopeKey(request.scope)
    const state = this.states.get(key) ?? EMPTY_STATE
    if (request.release) request.release(grant)
    this.states.set(key, {
      phase: 'failed',
      generation: state.generation,
      effective: request.release ? state.effective : grant,
      error,
      message: state.message,
    })
    return { ok: false, code: error.code, message: error.message, state: { ...this.states.get(key)! } }
  }

  private fail(key: string, generation: number, error: SelectionError): SelectionOutcome {
    const previous = this.states.get(key) ?? EMPTY_STATE
    this.states.set(key, {
      phase: 'failed',
      generation,
      // Kept deliberately: a failed attempt says what still applies, which is the
      // one thing a user needs after "that directory did not work". Nothing was
      // committed on this path, so the previous selection is genuinely intact.
      effective: previous.effective,
      error,
      message: previous.message,
    })
    return { ok: false, code: error.code, message: error.message, state: { ...this.states.get(key)! } }
  }
}

const EMPTY_STATE: SelectionState = {
  phase: 'idle',
  generation: 0,
  effective: null,
  error: null,
}

/** First usable path from a dialog result, or null. */
export function pickPath(result: SelectionDialogResult | null): string | null {
  if (!result || result.canceled) return null
  const paths = result.filePaths || []
  const first = paths[0]
  if (typeof first !== 'string' || !first) return null
  return first
}

function matchesScope(key: string, scope: Partial<GrantScope>): boolean {
  const parts = key.split('\u0001')
  const wanted = [scope.serverId, scope.userId, scope.tenantId, scope.deviceId]
  // Only the fields the caller named; an all-undefined partial clears everything.
  return wanted.every((value, index) => value == null || parts[index] === value)
}
