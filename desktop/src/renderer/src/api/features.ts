import type { FeatureActionEntry, FeatureActionMap } from '../types'

/**
 * The Desktop half of the per-action capability projection
 * (change `integrate-upstream-core-capabilities`, design D2,
 * implementation.md §3).
 *
 * The server publishes eight independently gated actions through
 * `GET /auth/context.feature_actions` and the Web console reads the same
 * declaration through `window.RdaiFunctionalCapabilities`. This module is the
 * Desktop equivalent: one pure predicate, no DOM, no request of its own, so the
 * API client can gate a business call before it reaches the broker.
 *
 * Two rules are deliberately strict:
 *
 * * `available` describes the *service*, never the caller. A `true` here only
 *   means "the client may ask"; the handler still resolves tenant/owner and
 *   resource authorization on every request.
 * * an old server that predates the field leaves every new capability closed.
 *   A missing field is "unknown", never "available" -- guessing from the
 *   presence of a menu entry is exactly the drift the server-side registry
 *   removes.
 */

/** The eight public action keys, in the server's own order. */
export type FeatureActionKey =
  | 'session_context.usage'
  | 'session_context.compact'
  | 'scheduler.instances'
  | 'scheduler.recipients'
  | 'scheduler.create'
  | 'scheduler.runs.list'
  | 'scheduler.runs.detail'
  | 'scheduler.runs.delete'

export const FEATURE_ACTION_KEYS: readonly FeatureActionKey[] = [
  'session_context.usage',
  'session_context.compact',
  'scheduler.instances',
  'scheduler.recipients',
  'scheduler.create',
  'scheduler.runs.list',
  'scheduler.runs.detail',
  'scheduler.runs.delete',
]

/** The subset of a `/auth/context` response this predicate reads. */
export interface FeatureActionContext {
  status?: string
  feature_actions?: FeatureActionMap | null
}

/** The entry for `key`, or null when the context does not declare it. */
function entryFor(
  context: FeatureActionContext | null | undefined,
  key: string,
): FeatureActionEntry | null {
  if (!context || context.status !== 'success') return null
  const actions = context.feature_actions
  if (!actions || typeof actions !== 'object') return null
  const entry = actions[key]
  if (!entry || typeof entry !== 'object') return null
  return entry
}

/**
 * True only when the context reports success AND the action's `available` is
 * strictly `true`. A missing field, a missing key, a non-success context or a
 * malformed truthy value (`'yes'`, `1`, `{}`) is `false`, never `true`.
 */
export function isFeatureAvailable(
  context: FeatureActionContext | null | undefined,
  key: string,
): boolean {
  const entry = entryFor(context, key)
  return !!entry && entry.available === true
}

/** Why an action is closed, or `''` when it is open (`'unknown'` if undeclared). */
export function featureReason(
  context: FeatureActionContext | null | undefined,
  key: string,
): string {
  const entry = entryFor(context, key)
  if (!entry) return 'unknown'
  if (entry.available === true) return ''
  return typeof entry.reason === 'string' ? entry.reason : 'unknown'
}

/**
 * Raised before a request is issued when the action is closed. It is distinct
 * from `ContextError`: the UI must show "not open in this deployment" rather
 * than "permission denied" or an empty result, and the caller must not retry.
 */
export class FeatureUnavailableError extends Error {
  constructor(
    public readonly key: string,
    public readonly reason = 'unknown',
  ) {
    super(`feature unavailable: ${key} (${reason})`)
    this.name = 'FeatureUnavailableError'
  }
}
