/**
 * The gate every effectful frame passes through on the device
 * (change ``align-desktop-project-execution-with-master``, tasks 7.1 - 7.6).
 *
 * The broker on the server decides whether a command *may* run. This module
 * decides whether it *may run here, now, once* -- and those are different
 * questions that only the device can answer:
 *
 *   * **once** -- the server cannot see this machine's disk between two frames,
 *     so the journal (task 7.1) is the only record of "I already began this".
 *     A redelivery with the same digest returns the stored result; a redelivery
 *     with a different digest is a conflict and is refused; a frame whose
 *     deadline has passed is expired rather than started late;
 *   * **now** -- a command that was lawful when queued can reach a device whose
 *     journal says the outcome is unknown, and the honest answer is to make the
 *     user inspect the files, not to run it again and hope;
 *   * **one at a time** -- ``effectful_per_project = 1`` is keyed on the *real*
 *     project root, so two sessions reaching the same directory through
 *     different workspace aliases serialise against each other (task 7.4).
 *     Read-only tools keep the contract's four-way parallelism.
 *
 * Cancellation is deliberately not "stop waiting": ``cancel`` resolves only
 * after the injected ``terminate`` reports the process tree has ended *and* the
 * run's own receipt has been written, so the verdict is recorded by the path
 * that owns the run rather than raced against it. The receipt it produces claims
 * the contract's own effects for ``cancelled`` -- which is ``unknown``, because a
 * stopped process never promises a rollback.
 *
 * The executor and the terminator are injected so this logic is testable, and
 * the journal is written *before* the executor is called and *before* the result
 * is returned. Nothing here reports a success it did not record.
 */

import {
  EFFECTFUL_TOOLS,
  EXECUTION_LIMITS,
  OUTCOME_UNKNOWN_CODE,
  effectsFor,
  validateExecuteFrame,
  type ExecutionEffects,
  type ExecutionResultFrame,
} from './contract'
import {
  JournalConflict,
  JournalDegraded,
  JournalStore,
  journalRetentionMs,
  type JournalEntry,
  type JournalReceipt,
  type JournalScope,
} from './journal'
import { FileVersions, stalenessWarning } from './file-versions'
import type { JournalRecord } from './generated-contract'

/** Who this device is, from its own native session -- never from the frame. */
export interface DeviceIdentity {
  origin: string
  user_id: string
  tenant_id: string
  device_id: string
}

/** What the executor is handed. It never sees the journal or the gate. */
export interface ExecutionRequest {
  frame: Record<string, unknown>
  /** Absolute real root, resolved here and never taken from the frame. */
  realRoot: string
  /** Absolute path of the project-relative working directory, if any. */
  cwd?: string
  /** Opaque per-run handle the terminator understands. */
  handle: CancelHandle
}

/** What one run reports back, before the gate turns it into a receipt. */
export interface ExecutionOutcome {
  exit_code: number
  stdout?: string
  stderr?: string
  error_code?: string
  error_message?: string
  duration_ms?: number
  /** Truncation, as the tool itself reported it (contract truncated). */
  truncated?: boolean
  /** The tool's own payload, carried through to the result frame. */
  payload?: ExecutionResultFrame['result']
  /** Set when the tool itself reported something more specific than an exit code. */
  phase?: 'succeeded' | 'failed' | 'cancelled' | 'expired' | 'outcome_unknown'
}

/**
 * The absolute paths one frame will modify, and the ones it hands back.
 *
 * Supplied by the adapter that knows the tool's arguments -- this module
 * deliberately never parses a payload, but it is the only place that can hold
 * the check *and* the journal record for one run, so the paths are handed in
 * rather than guessed at.
 */
export interface FramePaths {
  /** Files this frame will change: checked for an outside edit first (A20). */
  modifies?: string[]
  /** Files whose contents this frame returns: recorded as seen afterwards. */
  contents?: string[]
}

/** An opaque handle to something that can be genuinely stopped. */
export interface CancelHandle {
  commandId: string
  realRoot: string
}

/**
 * Stop the process tree and resolve only once it has ended.
 *
 * The real implementation is ``WorkerSession.cancelAndReap`` plus
 * ``killWorkerTree`` (task 7.5); resolving early would let the gate claim a
 * cancellation while the script is still writing files.
 */
export type Terminator = (handle: CancelHandle) => Promise<void>

export interface CommandRunnerOptions {
  journal: JournalStore
  /**
   * Resolve a workspace alias to the *real* project identity.
   *
   * Must collapse symlinks and normalise case/separators, because this string is
   * the serialisation key: an alias that resolves to the same directory must
   * share one key, or two sessions would write the same root concurrently.
   */
  realRootOf: (workspaceId: string) => string | null
  execute: (request: ExecutionRequest) => Promise<ExecutionOutcome>
  terminate: Terminator
  now?: () => number
  /**
   * Whether the device currently holds a live connection (task 7.5).
   *
   * Defaults to ``true``. When it returns ``false`` no new frame is admitted:
   * work accepted while offline could only be started against an authorization
   * the server can no longer re-check, and a *lost* connection is not a licence
   * to keep taking work.
   */
  connected?: () => boolean
  /**
   * Where the frame's own paths are declared (task 7.4, A20).
   *
   * The device's view of each file it serves is kept here so an edit against a
   * file that an outside writer changed is reported as ``file_changed`` instead
   * of silently overwriting them. Absent means "no version tracking", which is
   * the honest state for a tool that touches no file at all.
   */
  pathsOf?: (frame: Record<string, unknown>, realRoot: string) => FramePaths
  /** Where an unresolved entry is surfaced for reconciliation (task 7.3). */
  onReconcile?: (entry: JournalEntry, reason: string) => void
  /**
   * The liveness window, in ms. Defaults to the contract's own value; a caller
   * may override it the same way it overrides the call timeout.
   */
  livenessWindowMs?: number
  /** The record's ``journal_id`` source, injected so tests are deterministic. */
  newJournalId?: () => string
}

/** Why a frame was let through, or not. */
export type Admission =
  | { kind: 'run'; entry: JournalEntry; realRoot: string }
  | { kind: 'replay'; entry: JournalEntry; realRoot: string }
  | { kind: 'unresolved'; entry: JournalEntry; realRoot: string }
  | { kind: 'refused'; code: string; message: string }

/** What a caller may report to the server for one frame. */
export interface CommandResult {
  outcome: 'ran' | 'replayed' | 'unresolved' | 'refused' | 'cancelled'
  /** The contract phase a caller should report. */
  phase: string
  effects: ExecutionEffects
  entry: JournalEntry | null
  code: string
  message: string
  frame?: ExecutionResultFrame
  /** The tool's own payload, for the result frame the caller sends. */
  payload?: ExecutionResultFrame['result']
}

interface LiveRun {
  handle: CancelHandle
  started: boolean
  /** Resolves when the run's own path has written (or refused) its receipt. */
  settled: Promise<void>
}

/** A fair per-root scheduler: effectful commands run alone, reads share four. */
interface Gate {
  running: number
  effectful: boolean
  queue: Array<{ effectful: boolean; start: () => void }>
}

export class CommandRunner {
  private readonly journal: JournalStore
  private readonly now: () => number
  /** What this device last served for each file (task 7.4, A20). */
  readonly versions: FileVersions
  private readonly gates = new Map<string, Gate>()
  private readonly live = new Map<string, LiveRun>()
  /**
   * Commands a cancel asked to stop, set *before* the kill is attempted.
   *
   * Written before, not after: the tool's exit and the kill land on the same
   * event, so a flag set after the await would be read by the run path too late
   * and the killed run would record itself as a plain failure.
   */
  private readonly cancelRequested = new Set<string>()
  /** Sessions torn down by a sign-out or a tenant switch. */
  private readonly revoked = new Set<string>()

  constructor(private readonly options: CommandRunnerOptions) {
    this.journal = options.journal
    this.now = options.now ?? Date.now
    this.versions = new FileVersions()
  }

  /** Whether the journal is unreadable, in which case nothing effectful runs. */
  get degraded(): boolean {
    return this.journal.degraded
  }

  /**
   * Decide whether one frame may run, without running it.
   *
   * Separate from {@link run} because the decision is the interesting part: a
   * replay must return the *stored* result, an unresolved command must be
   * surfaced rather than retried, and a refusal must be reported with the
   * contract's own code -- there is no "probably fine" branch.
   */
  admit(frame: Record<string, unknown>, identity: DeviceIdentity): Admission {
    const commandId = String(frame.command_id ?? '')
    const workspaceId = String(frame.workspace_id ?? '')
    if (!commandId || !workspaceId) {
      return { kind: 'refused', code: 'invalid_request',
        message: 'the frame names no command or workspace' }
    }
    if (this.journal.degraded) {
      // No readable journal means this device cannot prove it never ran the
      // command. Failing closed is the only honest answer.
      return { kind: 'refused', code: 'journal_unavailable',
        message: this.journal.degradationReason }
    }
    if (this.options.connected && !this.options.connected()) {
      // A device that has lost its connection must not take new work: the
      // authorization behind the frame can no longer be re-checked, and a
      // result produced now could not be reported until a revalidation anyway.
      return { kind: 'refused', code: 'device_offline',
        message: 'the connection is down; work is not accepted until it is back' }
    }
    if (this.revoked.has(identityKey(identity))) {
      return { kind: 'refused', code: 'grant_revoked',
        message: 'the session this frame belongs to was signed out or switched' }
    }
    const realRoot = this.options.realRootOf(workspaceId)
    if (!realRoot) {
      return { kind: 'refused', code: 'stale_context',
        message: 'the workspace is not bound to this device' }
    }
    if (!this.isLive(frame)) {
      return { kind: 'refused', code: 'expired',
        message: 'the command is past its deadline and is not started late' }
    }
    const scope = this.scopeFor(frame, identity)
    const digest = String(frame.params_digest ?? '')
    const existing = this.journal.lookup(commandId, digest, scope)
    if (existing.kind === 'conflict') {
      return { kind: 'refused', code: 'command_conflict',
        message: 'this command id already has a record with a different payload' }
    }
    if (existing.kind === 'pruned') {
      // Its receipt aged out, but the command spent its authorization.
      return { kind: 'refused', code: 'command_conflict',
        message: 'this command id was already executed and its receipt was cleaned up' }
    }
    if (existing.kind === 'completed') {
      return { kind: 'replay', entry: existing.entry, realRoot }
    }
    if (existing.kind === 'started' || existing.kind === 'unknown') {
      this.options.onReconcile?.(
        existing.entry,
        'a previous attempt has no completion record; inspect the project '
        + 'before deciding, this command is not run again automatically')
      return { kind: 'unresolved', entry: existing.entry, realRoot }
    }

    const problems = validateExecuteFrame(
      frame, this.platformFor(frame))
    if (problems.length > 0) {
      return { kind: 'refused', code: 'invalid_request',
        message: problems.join('; ') }
    }

    try {
      const { entry } = this.journal.begin(
        this.recordFor(frame, identity), scope)
      return { kind: 'run', entry, realRoot }
    } catch (err) {
      if (err instanceof JournalConflict || err instanceof JournalDegraded) {
        return { kind: 'refused', code: 'command_conflict', message: err.message }
      }
      if (err instanceof Error) {
        return { kind: 'refused', code: 'invalid_request', message: err.message }
      }
      throw err
    }
  }

  /**
   * Run one frame: admit, serialise, execute, and record the receipt.
   *
   * The receipt is written before this method returns, and a run that throws
   * still writes one -- with ``outcome_unknown`` and ``effects: unknown`` --
   * because a tool that died mid-write is exactly the case where "it failed"
   * must not be read as "nothing happened".
   */
  async run(frame: Record<string, unknown>, identity: DeviceIdentity): Promise<CommandResult> {
    const admission = this.admit(frame, identity)
    if (admission.kind === 'refused') {
      const phase = this.refusalPhase(admission.code)
      return {
        outcome: 'refused', phase,
        // A refused command never reached the worker, so it claims no effect.
        effects: 'none',
        entry: null, code: admission.code, message: admission.message,
      }
    }
    if (admission.kind === 'replay') {
      const receipt = admission.entry.receipt
      return {
        outcome: 'replayed',
        phase: receipt?.outcome === 'succeeded' ? 'succeeded'
          : receipt?.outcome === 'cancelled' ? 'cancelled'
          : receipt?.outcome === 'expired' ? 'expired' : 'failed',
        effects: receipt?.effects ?? 'unknown',
        entry: admission.entry, code: '', message: 'already executed; returning the stored result',
        frame: receipt?.frame,
        ...(receipt?.payload ? { payload: receipt.payload } : {}),
      }
    }
    if (admission.kind === 'unresolved') {
      return {
        outcome: 'unresolved', phase: 'outcome_unknown', effects: 'unknown',
        entry: admission.entry, code: 'outcome_unknown',
        message: 'started with no recorded completion; the original files must be inspected',
      }
    }

    const tool = String(frame.tool ?? '')
    const effectful = (EFFECTFUL_TOOLS as readonly string[]).includes(tool)
    const realRoot = admission.realRoot

    // Registered *before* the gate, so a cancel that arrives while this run is
    // still queued behind another effectful command reaches the queue rather
    // than being told "nothing is running".
    const handle: CancelHandle = { commandId: String(frame.command_id), realRoot }
    let settle!: () => void
    const settledRun = new Promise<void>((resolve) => { settle = resolve })
    this.live.set(handle.commandId, { handle, started: false, settled: settledRun })
    try {
      return await this.schedule(realRoot, effectful, async () => {
        // A cancel that arrived while this run waited in the queue, or a second
        // frame that was already answered between admission and here, must not
        // execute. The journal is the authority, not an in-memory flag.
        const scope = this.scopeFor(frame, identity)
        const settled = this.journal.lookup(
          String(frame.command_id), String(frame.params_digest ?? ''), scope)
        if (settled.kind === 'completed') {
          return {
            outcome: 'cancelled', phase: 'cancelled',
            effects: settled.entry.receipt?.effects ?? 'none',
            entry: settled.entry, code: '', message: 'cancelled before it started',
          }
        }
        let outcome: ExecutionOutcome
        const paths = this.options.pathsOf?.(frame, realRoot) ?? {}
        // Checked *before* the tool runs: an outside edit is reported from the
        // version this device served, not from a diff of the result.
        const stale = this.versions.firstStale(paths.modifies ?? [])
        try {
          this.live.set(handle.commandId, { handle, started: true, settled: settledRun })
          outcome = await this.options.execute({ frame, realRoot, handle })
        } catch (err) {
          outcome = {
            exit_code: -1,
            error_code: 'tool_failed',
            error_message: (err as Error)?.message || 'the tool did not report a result',
            phase: 'outcome_unknown',
          }
        }

        // A change the model could not have seen is reported, never hidden. The
        // edit still applies -- the master's own rule, because refusing it would
        // strand the model -- so the contract's ``file_changed`` (effects
        // ``partial``) is exactly the right claim.
        if (stale) {
          const warning = stalenessWarning(stale.path, stale.reason)
          outcome = {
            ...outcome,
            error_code: outcome.error_code || 'file_changed',
            error_message: outcome.error_message
              ? `${outcome.error_message}\n${warning}` : warning,
          }
        }
        // Our own view is current again: a write is as good as a read, and a
        // served read is the whole point of the record. A run that *failed*
        // taught us nothing about the file, so its view is left as it was.
        if (outcome.exit_code === 0) {
          for (const path of [...(paths.contents ?? []), ...(paths.modifies ?? [])]) {
            this.versions.noteWritten(path)
          }
        }

        // A cancel that really stopped this run owns the verdict, because the
        // tool's own exit code cannot distinguish "killed" from "finished with a
        // bad status". A run that had already *succeeded* keeps its verdict: the
        // kill arrived after the work was done, and calling that cancelled would
        // be a lie about a complete result. The request is consumed here so a
        // later run of the same id does not inherit it.
        const cancelled = this.cancelRequested.delete(handle.commandId)
        const ownPhase = this.phaseOf(outcome)
        const phase = cancelled && ownPhase !== 'succeeded' ? 'cancelled' : ownPhase
        const effects = effectsFor(phase, phase === 'cancelled' ? undefined : outcome.error_code)
          ?? 'unknown'
        const receipt: JournalReceipt = {
          outcome: phase === 'succeeded' ? 'succeeded'
            : phase === 'cancelled' ? 'cancelled'
            : phase === 'expired' ? 'expired'
            : phase === 'outcome_unknown' ? 'outcome_unknown' : 'failed',
          effects,
          // The payload is part of the receipt, not a recomputation: a
          // redelivery has to return the same answer this run produced.
          ...(outcome.payload ? { payload: outcome.payload } : {}),
        }
        let entry: JournalEntry
        try {
          entry = this.journal.complete(
            String(frame.command_id), String(frame.params_digest ?? ''), receipt, scope)
        } catch (err) {
          // A receipt that cannot be written must not be reported as a success:
          // the effect happened, and the only record of it is missing.
          return {
            outcome: 'refused', phase: 'outcome_unknown', effects: 'unknown',
            entry: null, code: 'journal_unavailable',
            message: `the receipt could not be recorded: ${(err as Error).message}`,
          }
        }
        // What is *reported* is what is *recorded*, not what this run computed:
        // a receipt that already existed is the earlier, truthful verdict.
        const recorded = entry.receipt ?? receipt
        const recordedPhase = phaseForOutcome(recorded.outcome)
        return {
          outcome: recorded.outcome === 'cancelled' ? 'cancelled' : 'ran',
          phase: recordedPhase,
          effects: recorded.effects,
          entry,
          code: recordedPhase === phase ? (outcome.error_code ?? '') : '',
          message: recordedPhase === phase ? (outcome.error_message ?? '')
            : 'the recorded receipt was already written; it stands',
          // From the *recorded* receipt, so a redelivery answers with the
          // earlier payload rather than whatever this attempt produced.
          ...(recorded.payload ? { payload: recorded.payload } : {}),
        }
      })
    } finally {
      this.cancelRequested.delete(handle.commandId)
      this.live.delete(handle.commandId)
      settle()
    }
  }

  /**
   * Stop a running command, and only then call it cancelled.
   *
   * The *run's own path* writes the receipt: a cancel that wrote it here would
   * race the tool's exit, and whichever continuation happened to run first would
   * decide whether a killed script looked like a failed one. So this method
   * records the intent, waits for the tree to really end, and then waits for the
   * run to finish recording -- ``cancel`` returning ``true`` means the verdict is
   * already durable.
   *
   * A command that has not reached the worker is the one case with no race at
   * all, and it is the one case where the device knows more than the contract's
   * default for ``cancelled``: the queue never ran it, so it claims no effect.
   */
  async cancel(commandId: string, scope: JournalScope, paramsDigest: string): Promise<boolean> {
    const live = this.live.get(commandId)
    if (!live) {
      // Not running here. Either it never arrived, or it already finished --
      // and a finished command is not cancelled by asking nicely.
      return false
    }
    if (!live.started) {
      this.journal.complete(commandId, paramsDigest, {
        outcome: 'cancelled', effects: 'none',
      }, scope)
      return true
    }
    this.cancelRequested.add(commandId)
    await this.options.terminate(live.handle)
    await live.settled
    return true
  }

  /** Whether a command is running on this device right now. */
  isRunning(commandId: string): boolean {
    return this.live.has(commandId)
  }

  /**
   * Stop every run that outlived the connection (task 7.5, A19).
   *
   * A short outage is handled by the reconnect: the device asks the server for
   * ``status`` and re-runs the whole admission, and the journal makes that safe.
   * Past the liveness window that is no longer enough -- the run keeps writing
   * files that nothing will ever report -- so the tree is stopped here, and the
   * verdict recorded is the honest one: ``cancelled`` with ``unknown`` effects,
   * because a stopped process never promises a rollback. What it already wrote
   * stays on disk.
   *
   * Called by the connection supervisor on a timer, not by a frame: these runs
   * are exactly the ones with no one left to ask.
   */
  async abandonStaleRuns(
    options: { disconnectedForMs: number },
  ): Promise<JournalEntry[]> {
    const window = this.options.livenessWindowMs
      ?? EXECUTION_LIMITS.run_liveness_seconds * 1000
    if (options.disconnectedForMs <= window) return []
    const abandoned: JournalEntry[] = []
    for (const commandId of [...this.live.keys()]) {
      const entry = await this.stopRun(commandId)
      if (!entry) continue
      abandoned.push(entry)
      this.options.onReconcile?.(
        entry,
        'the connection was down past the liveness window, so this run was '
        + 'stopped locally; inspect the project for partial output')
    }
    return abandoned
  }

  /**
   * Tear down a session's running work: sign-out or a tenant switch (A18).
   *
   * The running process trees are terminated *first*, and only then does the
   * identity stop being accepted, so "signed out" cannot leave a script writing
   * into a project it was authorized for a moment ago. The receipts keep their
   * partial-effects claim: a stopped run is not a rolled-back one.
   */
  async revokeIdentity(identity: DeviceIdentity): Promise<number> {
    const key = identityKey(identity)
    const owned = new Set(
      this.journal.list()
        .filter((entry) => identityKey(entry) === key)
        .map((entry) => entry.command_id))
    let stopped = 0
    for (const commandId of [...this.live.keys()]) {
      if (!owned.has(commandId)) continue
      const entry = await this.stopRun(commandId)
      if (entry) stopped += 1
    }
    this.revoked.add(key)
    return stopped
  }

  /** Whether a frame from this session would still be accepted. */
  accepts(identity: DeviceIdentity): boolean {
    if (this.revoked.has(identityKey(identity))) return false
    if (this.options.connected && !this.options.connected()) return false
    return true
  }

  /** Terminate one live run and wait until its verdict is durable. */
  private async stopRun(commandId: string): Promise<JournalEntry | null> {
    const live = this.live.get(commandId)
    if (!live) return null
    const found = this.scopeAndEntry(commandId)
    if (!found) return null
    if (!live.started) {
      // Still queued: the worker never saw it, so there is nothing to kill and
      // no race to lose. The receipt is what makes the queued run skip itself.
      this.journal.complete(commandId, found.entry.params_digest, {
        outcome: 'cancelled', effects: 'none',
      }, found.scope)
      await live.settled
    } else {
      this.cancelRequested.add(commandId)
      await this.options.terminate(live.handle)
      await live.settled
    }
    return this.entryFor(commandId, found.scope) ?? null
  }

  /** The scope and record of a command this device has already journalled. */
  private scopeAndEntry(
    commandId: string,
  ): { scope: JournalScope; entry: JournalEntry } | null {
    const entry = this.journal.list().find((item) => item.command_id === commandId)
    if (!entry) return null
    return {
      scope: {
        origin: entry.origin,
        user_id: entry.user_id,
        tenant_id: entry.tenant_id,
        device_id: entry.device_id,
        command_id: entry.command_id,
      },
      entry,
    }
  }

  private entryFor(commandId: string, scope: JournalScope): JournalEntry | undefined {
    const found = this.journal.lookup(commandId, '', scope)
    return 'entry' in found ? found.entry : undefined
  }

  /**
   * What this device already knows about one command (task 7.2).
   *
   * A reconnect asks *before* re-sending, and the whole point is that the answer
   * comes from the journal rather than from running anything: a ``started``
   * entry is exactly the case the server must not re-drive, and an ``unknown``
   * one keeps its unknown verdict instead of being turned into a retry by a
   * helpful-looking default. ``null`` means "no record", which is the one state
   * a caller may safely treat as "not started here".
   */
  status(
    commandId: string,
    scope: JournalScope,
  ): { state: string; phase?: string; effects?: ExecutionEffects; result?: unknown } | null {
    const found = this.journal.lookup(commandId, '', scope)
    if (!('entry' in found)) return null
    const entry = found.entry
    if (entry.state === 'started') return { state: 'running' }
    if (entry.state === 'unknown') {
      return { state: 'failed', phase: OUTCOME_UNKNOWN_CODE, effects: 'unknown' }
    }
    const receipt = entry.receipt
    if (!receipt) return { state: 'running' }
    return {
      state: stateForReceipt(receipt.outcome),
      phase: phaseForOutcome(receipt.outcome),
      effects: receipt.effects,
      ...(receipt.payload ?? receipt.frame
        ? { result: receipt.payload ?? receipt.frame } : {}),
    }
  }

  /**
   * Mark everything unfinished as ``outcome_unknown`` and report it.
   *
   * Called once at startup. Nothing here is retried: the whole point of the
   * unknown state is that a later human decides.
   */
  recoverUnfinished(): JournalEntry[] {
    const before = new Map(this.journal.list().map((entry) => [entry.journal_id, entry]))
    this.journal.recover()
    const unresolved: JournalEntry[] = []
    for (const entry of this.journal.list()) {
      if (entry.state !== 'unknown') continue
      if (before.get(entry.journal_id)?.state === 'unknown') continue
      unresolved.push(entry)
      this.options.onReconcile?.(
        entry, 'the process stopped between the start intent and the receipt')
    }
    return unresolved
  }

  /**
   * Retention: age out old receipts, then enforce the record cap.
   *
   * ``outcome_unknown`` records are never reclaimed -- the journal's ``prune``
   * refuses to, because that record is the only evidence the command might have
   * had an effect, and a later reconciliation still needs it.
   */
  sweepRetention(options: { retainDays?: number; keepAtMost?: number } = {}): string[] {
    const retainDays = options.retainDays ?? EXECUTION_LIMITS.journal_retain_days
    const window = journalRetentionMs(retainDays)
    const cutoff = this.now() - window
    const removed = this.journal.prune(cutoff, { keepAtMost: options.keepAtMost })
    // Tombstones are bounded by twice the window: they only have to outlive the
    // redelivery window, not the receipts they replaced.
    this.journal.pruneTombstones(this.now() - window * 2)
    return removed
  }

  // -- internals ---------------------------------------------------------

  /**
   * Queue one run behind the root's other work.
   *
   * A synchronous admission step, not a promise chain: a chain would let five
   * reads all observe "four are running" before any of them incremented, which
   * is exactly the limit it is supposed to enforce.
   */
  private schedule<T>(
    realRoot: string,
    effectful: boolean,
    work: () => Promise<T>,
  ): Promise<T> {
    const gate = this.gates.get(realRoot)
      ?? { running: 0, effectful: false, queue: [] }
    this.gates.set(realRoot, gate)
    return new Promise<T>((resolve, reject) => {
      gate.queue.push({
        effectful,
        start: () => {
          gate.running += 1
          if (effectful) gate.effectful = true
          let running: Promise<T>
          try {
            running = work()
          } catch (err) {
            running = Promise.reject(err)
          }
          running.then(resolve, reject).finally(() => {
            gate.running -= 1
            if (effectful) gate.effectful = false
            this.drain(gate)
          })
        },
      })
      this.drain(gate)
    })
  }

  /** Start whatever the root's limits allow, in arrival order. */
  private drain(gate: Gate): void {
    const limit = EXECUTION_LIMITS.readonly_parallel_per_project
    while (gate.queue.length > 0) {
      const head = gate.queue[0]
      const allowed = head.effectful
        // An effectful command owns the root: no other side effect, and no read
        // racing it, so a script never sees a file mid-write from a peer.
        ? gate.running === 0
        : !gate.effectful && gate.running < limit
      if (!allowed) return
      gate.queue.shift()
      head.start()
    }
  }

  private scopeFor(frame: Record<string, unknown>, identity: DeviceIdentity): JournalScope {
    return {
      origin: identity.origin,
      user_id: identity.user_id,
      tenant_id: identity.tenant_id,
      device_id: identity.device_id,
      command_id: String(frame.command_id ?? ''),
    }
  }

  private recordFor(frame: Record<string, unknown>, identity: DeviceIdentity): JournalRecord {
    const skills = Array.isArray(frame.skill_resources)
      ? (frame.skill_resources as Array<Record<string, unknown>>)
        .map((entry) => String(entry?.digest ?? '')).filter(Boolean).sort()
      : []
    return {
      journal_id: (this.options.newJournalId ?? defaultJournalId)(),
      command_id: String(frame.command_id),
      params_digest: String(frame.params_digest ?? ''),
      origin: identity.origin,
      user_id: identity.user_id,
      tenant_id: identity.tenant_id,
      device_id: identity.device_id,
      workspace_id: String(frame.workspace_id ?? ''),
      binding_id: frame.binding_id ? String(frame.binding_id) : undefined,
      run_id: String(frame.run_id ?? ''),
      tool_call_id: String(frame.tool_call_id ?? ''),
      tool: String(frame.tool ?? ''),
      grant_version: Number(frame.grant_version ?? 0),
      started_at: this.now(),
      connection_epoch: frame.connection_epoch ? String(frame.connection_epoch) : undefined,
      permit_id: frame.permit_id ? String(frame.permit_id) : undefined,
      skill_digests: skills,
    }
  }

  private platformFor(frame: Record<string, unknown>): 'posix' | 'win32' {
    return frame.platform === 'win32' ? 'win32' : 'posix'
  }

  private phaseOf(outcome: ExecutionOutcome): string {
    if (outcome.phase) return outcome.phase
    return outcome.exit_code === 0 ? 'succeeded' : 'failed'
  }

  private refusalPhase(code: string): string {
    if (code === 'expired') return 'expired'
    return 'failed'
  }

  /** Whether the frame's own deadline has passed. Never start late. */
  private isLive(frame: Record<string, unknown>): boolean {
    const expires = frame.expires_at
    if (typeof expires === 'string' && expires.trim() !== '') {
      const at = Date.parse(expires)
      if (!Number.isNaN(at)) return this.now() <= at
    }
    return true
  }
}

function defaultJournalId(): string {
  const { randomBytes } = require('crypto') as typeof import('crypto')
  return `journal_${randomBytes(18).toString('base64url')}`
}

/** The session a journal record or a device identity belongs to. */
function identityKey(identity: { user_id: string; tenant_id: string }): string {
  return `${identity.user_id}\u0000${identity.tenant_id}`
}

/** A journal outcome, back to the phase a caller reports. */
function phaseForOutcome(outcome: string): string {
  switch (outcome) {
    case 'succeeded': return 'succeeded'
    case 'cancelled': return 'cancelled'
    case 'expired': return 'expired'
    case 'outcome_unknown': return 'outcome_unknown'
    default: return 'failed'
  }
}

/** The persisted v1 state a stored receipt maps to (``unknown`` is a failure). */
function stateForReceipt(outcome: string): string {
  if (outcome === 'outcome_unknown') return 'failed'
  return outcome
}
