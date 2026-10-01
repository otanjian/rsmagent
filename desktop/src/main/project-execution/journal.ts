/**
 * The main process's durable execution journal (task 7.1 / 7.2 / 7.6).
 *
 * The journal is not a second copy of the business task list: it holds one
 * record per *effectful* command, keyed by the contract's own dedup key
 * (``origin``/``user``/``tenant``/``device``/``command_id``), and it answers
 * exactly three questions that nothing else on this machine can:
 *
 *   * was this command's payload the one I was authorized for? (a same id with
 *     a different ``params_digest`` is ``command_conflict``, never executed);
 *   * did I begin it? -- the start intent is written **before** the worker can
 *     touch the project, so a crash leaves evidence instead of a silence;
 *   * how did it end? -- the receipt is written **before** it is reported, so a
 *     redelivery after a lost frame returns the stored answer instead of
 *     running the effect twice.
 *
 * The one thing it must never do is invent an answer. A start intent with no
 * receipt is marked ``outcome_unknown`` on recovery and is **never** retried
 * automatically: the contract is explicit that a generic script has no
 * exactly-once guarantee across a crash, and pretending otherwise is worse than
 * saying "inspect the files".
 *
 * Pure Node (path and clock injected, no Electron) so the crash, redelivery and
 * retention paths are exercised without a window.
 */

import { existsSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'fs'
import * as path from 'path'

import {
  JOURNAL_CONFLICT_CODE,
  JOURNAL_REQUIRED,
  type ExecutionEffects,
  type ExecutionResultFrame,
  type JournalRecord,
} from './generated-contract'

/** Where a command stands, from this machine's point of view. */
export type JournalState = 'started' | 'unknown' | 'completed'

/** How a command ended, in the contract's own vocabulary. */
export type JournalOutcome =
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'expired'
  | 'outcome_unknown'

/** The receipt persisted before the result is reported. */
export interface JournalReceipt {
  outcome: JournalOutcome
  effects: ExecutionEffects
  /** The exact frame reported to the server, when one was built. */
  frame?: ExecutionResultFrame
  /**
   * The tool's own payload, as this end's tool produced it (contract §frames).
   *
   * Stored with the receipt because a redelivery must return the *same* answer:
   * re-deriving it from a tool that is no longer running is how a model sees two
   * different results for one call.
   */
  payload?: ExecutionResultFrame['result']
}

/** One journal record, with this machine's own state on top of the contract's. */
export interface JournalEntry extends JournalRecord {
  state: JournalState
  completed_at: number | null
  receipt: JournalReceipt | null
}

/** The dedup key of a command, as a single string. */
export interface JournalScope {
  origin: string
  user_id: string
  tenant_id: string
  device_id: string
  command_id: string
}

/** Same id, different payload: the one redelivery that must never execute. */
export class JournalConflict extends Error {
  readonly code = JOURNAL_CONFLICT_CODE

  constructor(commandId: string) {
    super(
      `command ${commandId} already has a record with a different payload ` +
        `(${JOURNAL_CONFLICT_CODE}); it is refused, not executed as the new payload`,
    )
    this.name = 'JournalConflict'
  }
}

/** The journal itself is unreadable: effectful commands fail closed. */
export class JournalDegraded extends Error {
  constructor(reason: string) {
    super(`execution journal unavailable: ${reason}`)
    this.name = 'JournalDegraded'
  }
}

/** What ``lookup`` answers, and what a caller must do about it. */export type JournalLookup =
  | { kind: 'none' }
  | { kind: 'conflict'; entry: JournalEntry }
  | { kind: 'completed'; entry: JournalEntry }
  | { kind: 'started'; entry: JournalEntry }
  | { kind: 'unknown'; entry: JournalEntry }
  | { kind: 'pruned'; key: string }

export interface JournalStoreOptions {
  /** Absolute path of the journal file (under the user-data directory). */
  file: string
  now?: () => number
  /** Injected so tests can pin ``journal_id`` values. */
  randomId?: () => string
}

interface PrunedMarker {
  key: string
  pruned_at: number
}

interface JournalFile {
  version: number
  entries: JournalEntry[]
  pruned: PrunedMarker[]
}

const FILE_VERSION = 1

/** The dedup key of one command, from the contract's own field list. */
export function dedupKey(scope: JournalScope): string {
  return [scope.origin, scope.user_id, scope.tenant_id, scope.device_id,
          scope.command_id].join('\u0000')
}

function entryKey(record: Pick<JournalRecord, 'origin' | 'command_id'>,
                  scope?: Partial<JournalScope>): string {
  return dedupKey({
    origin: record.origin,
    user_id: scope?.user_id ?? '',
    tenant_id: scope?.tenant_id ?? '',
    device_id: scope?.device_id ?? '',
    command_id: record.command_id,
  })
}

/**
 * The local start journal, one instance per user-data directory.
 *
 * Every mutation writes the whole file via a temp file + rename, so a crash
 * leaves either the previous state or the new one -- never a half-written
 * record that a recovery pass would misread.
 */
export class JournalStore {
  private readonly file: string
  private readonly now: () => number
  private readonly randomId: () => string
  private readonly entries = new Map<string, JournalEntry>()
  private pruned: PrunedMarker[] = []
  private loaded = false
  private degradedReason = ''

  constructor(options: JournalStoreOptions) {
    this.file = options.file
    this.now = options.now ?? Date.now
    this.randomId = options.randomId ?? (() => defaultJournalId())
    this.loadFromDisk()
  }

  // -- the write half ----------------------------------------------------

  /**
   * Persist the start intent, before the worker can touch the project.
   *
   * Idempotent for a genuine redelivery (same id, same digest): the existing
   * record is returned and nothing is written again. A same-id record with a
   * different digest is a conflict and is refused.
   */
  begin(record: JournalRecord, scope: JournalScope): { entry: JournalEntry; created: boolean } {
    assertComplete(record)
    if (this.degradedReason) {
      // The journal file could not be read, so this machine cannot prove it
      // never ran the command. Executing anyway is exactly the doubled effect
      // the journal exists to prevent, so an effectful command is refused until
      // an operator reconciles the quarantined file.
      throw new JournalDegraded(this.degradedReason)
    }
    assertScopeMatches(record, scope)
    const key = dedupKey(scope)
    const existing = this.findByKey(key)
    if (existing) {
      if (existing.params_digest !== record.params_digest) {
        throw new JournalConflict(record.command_id)
      }
      return { entry: existing, created: false }
    }
    if (this.prunedKeys().has(key)) {
      // A receipt this machine already cleaned up. The command must not become
      // executable again just because its evidence aged out.
      throw new JournalConflict(record.command_id)
    }
    const entry: JournalEntry = {
      ...record,
      journal_id: record.journal_id || this.randomId(),
      started_at: record.started_at || this.now(),
      state: 'started',
      completed_at: null,
      receipt: null,
    }
    this.entries.set(key, entry)
    this.persist()
    return { entry, created: true }
  }

  /**
   * Persist the completion receipt, before the result is reported.
   *
   * ``complete`` requires a start intent: a completion without one would claim a
   * start this machine never recorded, which is exactly the silence the journal
   * exists to prevent.
   */
  complete(
    commandId: string,
    paramsDigest: string,
    receipt: JournalReceipt,
    scope: JournalScope,
  ): JournalEntry {
    const key = dedupKey(scope)
    const existing = this.findByKey(key)
    if (!existing) {
      throw new Error(`no start intent for command ${commandId}; refusing to record a receipt`)
    }
    if (existing.params_digest !== paramsDigest) {
      throw new JournalConflict(commandId)
    }
    if (existing.state === 'completed') {
      // A second receipt for the same command is not a second effect: return
      // the stored one so a retried report cannot overwrite the truth.
      return existing
    }
    const entry: JournalEntry = {
      ...existing,
      state: 'completed',
      completed_at: this.now(),
      receipt,
    }
    this.entries.set(key, entry)
    this.persist()
    return entry
  }

  // -- the read half -----------------------------------------------------

  /** What this machine knows about one command. */
  lookup(commandId: string, paramsDigest: string, scope: JournalScope): JournalLookup {
    const key = dedupKey(scope)
    const existing = this.findByKey(key)
    if (existing) {
      if (paramsDigest && existing.params_digest !== paramsDigest) {
        return { kind: 'conflict', entry: existing }
      }
      if (existing.state === 'completed') return { kind: 'completed', entry: existing }
      if (existing.state === 'unknown') return { kind: 'unknown', entry: existing }
      return { kind: 'started', entry: existing }
    }
    if (this.prunedKeys().has(key)) return { kind: 'pruned', key }
    return { kind: 'none' }
  }

  /** Every record, in insertion order. A defensive copy. */
  list(): JournalEntry[] {
    return [...this.entries.values()].map((entry) => ({ ...entry }))
  }

  // -- crash recovery and retention --------------------------------------

  /**
   * Mark every unfinished start as ``outcome_unknown``.
   *
   * Run once at startup, before the gateway is consumed. An entry in
   * ``started`` means the intent reached disk but no receipt did -- the process
   * died in between -- and the contract forbids guessing: it is not "not
   * executed" and it is not "succeeded". It is deliberately **not** retried.
   *
   * ``completed_at`` here is when the record stopped being live (the moment the
   * machine concluded the outcome is unknowable), not when the command ended --
   * which is exactly what nobody knows. It is recorded so retention has a
   * timestamp to reason about, while the ``unknown`` state keeps the record from
   * ever being reclaimed by ``prune``.
   */
  recover(): number {
    let changed = 0
    for (const [key, entry] of this.entries) {
      if (entry.state !== 'started') continue
      this.entries.set(key, {
        ...entry,
        state: 'unknown',
        completed_at: this.now(),
        receipt: { outcome: 'outcome_unknown', effects: 'unknown' },
      })
      changed += 1
    }
    if (changed > 0) this.persist()
    return changed
  }

  /**
   * Drop receipts older than ``retainBefore``, leaving tombstones behind.
   *
   * The ``state === 'completed'`` guard is what protects the unknown outcomes:
   * an ``unknown`` record carries a ``completed_at`` too (recovery sets it), so
   * it is the *state*, not the timestamp, that keeps it. Reclaiming an unknown
   * would destroy the only evidence that the command might have had an effect --
   * exactly the case a later reconciliation still needs, and the one where
   * "the receipt is gone" means "assume it ran". Tombstones keep the dedup key,
   * so a pruned command stays non-executable instead of becoming a fresh one.
   */
  prune(retainBefore: number, options: { keepAtMost?: number } = {}): string[] {
    const removed: string[] = []
    const survivors: PrunedMarker[] = []
    for (const [key, entry] of this.entries) {
      if (entry.state !== 'completed') continue
      const finishedAt = entry.completed_at
      if (finishedAt === null || finishedAt >= retainBefore) continue
      removed.push(entry.journal_id)
      this.entries.delete(key)
      survivors.push({ key, pruned_at: this.now() })
    }
    // The age window is the intent; the cap is the backstop. A device that was
    // handed far more commands than its retention window anticipated must not
    // grow an unbounded file, so the *oldest finished* records go first -- and
    // only finished ones, for the same reason the state guard exists.
    const keepAtMost = options.keepAtMost ?? 0
    if (keepAtMost > 0) {
      const finished = [...this.entries.entries()]
        .filter(([, entry]) => entry.state === 'completed')
        .sort((a, b) => (a[1].completed_at ?? 0) - (b[1].completed_at ?? 0))
      let over = this.entries.size - keepAtMost
      for (const [key, entry] of finished) {
        if (over <= 0) break
        removed.push(entry.journal_id)
        this.entries.delete(key)
        survivors.push({ key, pruned_at: this.now() })
        over -= 1
      }
    }
    if (removed.length > 0) {
      if (this.pruned.length > 0) survivors.push(...this.pruned)
      this.pruned = survivors
      this.persist()
    }
    return removed
  }

  /** Tombstones themselves are bounded too: nothing is kept forever. */
  pruneTombstones(retainBefore: number): number {
    const kept = this.pruned.filter((marker) => marker.pruned_at >= retainBefore)
    const removed = this.pruned.length - kept.length
    if (removed > 0) {
      this.pruned = kept
      this.persist()
    }
    return removed
  }

  // -- internals ---------------------------------------------------------

  private findByKey(key: string): JournalEntry | undefined {
    return this.entries.get(key)
  }

  private prunedKeys(): Set<string> {
    return new Set(this.pruned.map((marker) => marker.key))
  }

  private loadFromDisk(): void {
    if (this.loaded) return
    this.loaded = true
    if (!existsSync(this.file)) return
    let raw: Partial<JournalFile>
    try {
      raw = JSON.parse(readFileSync(this.file, 'utf8')) as Partial<JournalFile>
    } catch (err) {
      // Keep the unreadable bytes for an operator, and fail closed: with no
      // readable journal this machine cannot rule out a doubled effect.
      const quarantined = `${this.file}.corrupt`
      try {
        renameSync(this.file, quarantined)
      } catch {
        /* if it cannot be moved it is still reported below */
      }
      this.degradedReason =
        `execution journal is unreadable (${(err as Error).message}); ` +
        `moved to ${quarantined}`
      return
    }
    for (const entry of raw.entries ?? []) {
      if (entry && typeof entry.command_id === 'string') {
        this.entries.set(
          entryKey(entry, entry as unknown as Partial<JournalScope>), entry)
      }
    }
    this.pruned = (raw.pruned ?? []).filter(
      (marker): marker is PrunedMarker => !!marker && typeof marker.key === 'string',
    )
  }

  /** True when the journal could not be read and refuses effectful commands. */
  get degraded(): boolean {
    return this.degradedReason !== ''
  }

  /** Why effectful commands are being refused, or an empty string. */
  get degradationReason(): string {
    return this.degradedReason
  }

  private persist(): void {
    mkdirSync(path.dirname(this.file), { recursive: true })
    const body: JournalFile = {
      version: FILE_VERSION,
      entries: [...this.entries.values()],
      pruned: this.pruned,
    }
    const tmp = `${this.file}.${process.pid}.tmp`
    try {
      writeFileSync(tmp, JSON.stringify(body), { mode: 0o600 })
      renameSync(tmp, this.file)
    } catch (err) {
      try {
        unlinkSync(tmp)
      } catch {
        /* the temp file may not exist */
      }
      throw err
    }
  }
}

function assertComplete(record: JournalRecord): void {
  for (const key of JOURNAL_REQUIRED) {
    const value = (record as unknown as Record<string, unknown>)[key]
    if (value === undefined || value === null || value === '') {
      throw new Error(`journal record is missing '${key}'`)
    }
  }
}

/**
 * The dedup identity must come from the record's own fields.
 *
 * The key is rebuilt from the record when the journal is read back, so a caller
 * that passed a *different* device or user than the record carries would write
 * under one key and look under another -- a redelivery that silently looks
 * unseen. Refusing the mismatch is cheaper than debugging that.
 */
function assertScopeMatches(record: JournalRecord, scope: JournalScope): void {
  for (const field of ['origin', 'user_id', 'tenant_id', 'device_id', 'command_id'] as const) {
    if (record[field] !== scope[field]) {
      throw new Error(
        `journal scope disagrees with the record on '${field}' ` +
          `(record '${record[field]}', scope '${scope[field]}')`,
      )
    }
  }
}

function defaultJournalId(): string {
  // 18 random bytes, base64url: an opaque local id, never a credential.
  const { randomBytes } = require('crypto') as typeof import('crypto')
  return `journal_${randomBytes(18).toString('base64url')}`
}

/** The journal file for a desktop user-data directory. */
export function journalPath(userDataDir: string): string {
  return path.join(userDataDir, 'execution-journal.json')
}

/** Receipt retention in milliseconds, from the contract's own ``retain_days``. */
export function journalRetentionMs(retainDays: number): number {
  return Math.max(1, Math.floor(retainDays)) * 24 * 60 * 60 * 1000
}
