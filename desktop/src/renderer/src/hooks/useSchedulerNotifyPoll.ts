import { useEffect, useSyncExternalStore } from 'react'
import apiClient from '../api/client'
import { notifyScheduledRun } from '../lib/taskNotify'
import desktopContext, { ContextError } from '../api/context'
import { isFeatureAvailable, FeatureUnavailableError } from '../api/features'
import type { SchedulerRun } from '../types'

// Poll the GLOBAL runs ledger for scheduled executions, independent of which
// session is active. usePushPoll only watches the currently open session, so a
// scheduled task firing into any other session (the common case for reminders)
// would never surface. This loop asks "any scheduled execution since I last
// checked?" and is the SINGLE source of scheduler notifications: it forces a
// notice for every client-delivered execution — timer or manual "run now",
// current session or not — and dedupes by run id (usePushPoll deliberately
// stays silent for scheduler pushes). The delivered body is already persisted
// to the session history, so clicking the notification (open-session) loads it.
//
// It is gated on `scheduler.runs.list` (design D2): a closed action must not
// issue any request, and a permission/service refusal must stop the loop rather
// than being retried forever or shown as "no records".
export const POLL_INTERVAL_MS = 10000
export const PAGE_LIMIT = 20
// Bound the "drain a full page" loop: a pathological server that always returns
// a full page must not spin. 25 * 20 = 500 runs per tick is far past any real
// same-second burst.
export const MAX_PAGES = 25

// Module-level guard so only ONE loop runs process-wide (StrictMode double-mount
// in dev, or the hook accidentally mounted twice, would otherwise notify twice).
let loopActive = false

// Runs we've already notified for, so overlapping polls (or a run that lingers
// in the `since` window across two ticks) never double-fire a notification.
// Cleared whenever the identity/tenant/connection is invalidated.
const notifiedRunIds = new Set<string>()

/** The poll's user-visible state, so a banner can distinguish pause reasons. */
export interface SchedulerNotifyState {
  /** The feature is open and the poll is allowed to run. */
  available: boolean
  /** True after a permission/service error stopped the loop. */
  paused: boolean
  /** '' while healthy; a short reason while paused. */
  reason: string
}

let notifyState: SchedulerNotifyState = { available: false, paused: false, reason: '' }
const notifyListeners = new Set<() => void>()

function patchNotifyState(patch: Partial<SchedulerNotifyState>): void {
  const next: SchedulerNotifyState = {
    available: patch.available ?? notifyState.available,
    paused: patch.paused ?? notifyState.paused,
    reason: patch.reason ?? notifyState.reason,
  }
  if (
    next.available === notifyState.available &&
    next.paused === notifyState.paused &&
    next.reason === notifyState.reason
  ) {
    return
  }
  notifyState = next
  for (const listener of notifyListeners) listener()
}

export function getSchedulerNotifyState(): SchedulerNotifyState {
  return notifyState
}

export function subscribeSchedulerNotify(listener: () => void): () => void {
  notifyListeners.add(listener)
  return () => notifyListeners.delete(listener)
}

// ---------------------------------------------------------------------------
// Pure decision logic
//
// The effect below is the only part that touches a timer or the network; these
// functions hold every decision it makes, so they can be tested directly. Each
// one is exported only for that reason.
// ---------------------------------------------------------------------------

/** What one failed tick should do next. */
export type PollErrorAction = 'retry' | 'pause'

/**
 * Only a network-level failure is worth retrying: the request never reached the
 * server, so re-issuing the SAME `since`/`offset` window is safe and cannot lose
 * runs. Everything else -- a permission refusal, a closed action, a service
 * fault -- is `pause`. The decision deliberately carries no cursor: a refusal
 * must never widen the query range to get an answer.
 */
export function pollErrorDecision(err: unknown): PollErrorAction {
  return err instanceof ContextError && err.kind === 'network' ? 'retry' : 'pause'
}

/** One step of the `since`-window drain. */
export interface DrainStep {
  /** Offset for the next request in the same `since` window. */
  nextOffset: number
  /** A short page ends the drain: the window has no more runs. */
  done: boolean
}

/**
 * Decide whether to fetch another page of the same `since` window. A full page
 * means there may be more runs at this second, so continue from `offset + size`;
 * a short page ends the drain. `MAX_PAGES` bounds the caller's loop.
 */
export function nextDrainStep(pageSize: number, offset: number): DrainStep {
  if (pageSize < PAGE_LIMIT) return { nextOffset: offset, done: true }
  return { nextOffset: offset + pageSize, done: false }
}

/**
 * Drain every run in one `since` window by paging `offset`, so several runs in
 * the same second are all collected before the cursor advances. `fetchPage` is
 * the paged reader and `isCancelled` lets a teardown abandon an in-flight drain
 * (returning null) instead of applying a previous identity's result.
 */
export async function collectRunPages(
  fetchPage: (offset: number) => Promise<SchedulerRun[]>,
  isCancelled: () => boolean = () => false,
): Promise<SchedulerRun[] | null> {
  const collected: SchedulerRun[] = []
  let offset = 0
  for (let page = 0; page < MAX_PAGES; page++) {
    const runs = await fetchPage(offset)
    if (isCancelled()) return null
    collected.push(...runs)
    const step = nextDrainStep(runs.length, offset)
    if (step.done) break
    offset = step.nextOffset
  }
  return collected
}

/** Oldest-first, with run_id as the stable tiebreaker for same-second runs. */
export function orderRunsOldestFirst(runs: SchedulerRun[]): SchedulerRun[] {
  return runs.slice().sort((a, b) => {
    const byTime = (a.started_at || 0) - (b.started_at || 0)
    if (byTime !== 0) return byTime
    const aId = a.run_id || ''
    const bId = b.run_id || ''
    return aId < bId ? -1 : aId > bId ? 1 : 0
  })
}

/** Advance the `since` cursor monotonically: it must never move backwards. */
export function advanceSince(since: number, ordered: SchedulerRun[]): number {
  let next = since
  for (const run of ordered) {
    if (run.started_at && run.started_at > next) next = run.started_at
  }
  return next
}

/**
 * Only client-delivered executions warrant a desktop notification: WeChat,
 * Feishu, ... already push into their own app. Runs still executing (or failed)
 * are skipped -- there is nothing delivered to jump to yet. `trigger` is
 * deliberately ignored so a timer tick and a manual "run now" both announce.
 */
export function isNotifiableRun(run: Pick<SchedulerRun, 'channel_type' | 'status'>): boolean {
  const channel = run.channel_type || ''
  if (channel && channel !== 'web') return false
  if (run.status && run.status !== 'done') return false
  return true
}

/** The cap on remembered run ids; trips only on an absurd run rate. */
export const SEEN_RUN_LIMIT = 500

/** Record a notified run id, evicting the oldest ids past the memory bound. */
export function rememberNotifiedRun(seen: Set<string>, runId: string): void {
  if (!runId) return
  seen.add(runId)
  if (seen.size <= SEEN_RUN_LIMIT) return
  const drop = seen.size - SEEN_RUN_LIMIT
  let i = 0
  for (const id of seen) {
    if (i++ >= drop) break
    seen.delete(id)
  }
}

/** The notifications for one drained window plus the advanced cursor. */
export interface NotifyBatch {
  /** Runs to announce, oldest-first and deduped by run_id. */
  runs: SchedulerRun[]
  /** The new `since`; never smaller than the old one. */
  since: number
}

/**
 * Turn one drained window into the notifications to fire and the next cursor.
 *
 * `seen` is mutated: every run returned is remembered, so an overlapping window
 * (or the same-second re-read that `started_at >= since` performs) never
 * double-fires. Runs without a `run_id` still notify -- there is nothing to
 * dedupe them by, and dropping a real execution would be worse.
 */
export function planNotifyBatch(
  runs: SchedulerRun[],
  since: number,
  seen: Set<string>,
): NotifyBatch {
  const ordered = orderRunsOldestFirst(runs)
  const notify: SchedulerRun[] = []
  for (const run of ordered) {
    if (!run.session_id || !isNotifiableRun(run)) continue
    const runId = run.run_id || ''
    if (runId && seen.has(runId)) continue
    rememberNotifiedRun(seen, runId)
    notify.push(run)
  }
  return { runs: notify, since: advanceSince(since, ordered) }
}

export function useSchedulerNotifyPoll(ready: boolean): void {
  const snapshot = useSyncExternalStore(desktopContext.subscribe, desktopContext.getSnapshot)
  const featureAvailable =
    snapshot.gate === 'ready' &&
    !!snapshot.session &&
    isFeatureAvailable(
      { status: snapshot.featureActions ? 'success' : 'error', feature_actions: snapshot.featureActions },
      'scheduler.runs.list',
    )
  const allowed = ready && featureAvailable
  // Any identity/tenant/connection change moves one of these, so the loop tears
  // down (clearing the timer and the seen set) instead of reusing old state.
  const epoch = snapshot.session?.epoch ?? 0
  const tenantId = snapshot.session?.tenantId ?? ''
  const revision = snapshot.featureRevision

  useEffect(() => {
    if (!allowed || loopActive) return
    loopActive = true
    // A restart under a new identity must not treat the previous identity's
    // runs as already-seen.
    notifiedRunIds.clear()
    patchNotifyState({ available: true, paused: false, reason: '' })
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    // Ignore anything that ran before this client came up: on first tick we only
    // want NEW executions, not a backlog of history replayed as notifications.
    let since = Math.floor(Date.now() / 1000)

    const stopTimer = () => {
      if (timer) {
        clearTimeout(timer)
        timer = null
      }
    }

    const schedule = () => {
      if (cancelled) return
      stopTimer()
      timer = setTimeout(tick, POLL_INTERVAL_MS)
    }

    // A refusal is not transient: stop the loop completely. A permission
    // refusal or a service fault must not be retried on the normal interval,
    // and re-issuing the same window would not help. No further request is
    // issued here. The loop starts again only when the context is
    // re-established -- a reconnect invalidates the projection, bumps the
    // revision and re-runs this effect, which re-fetches the projection.
    const pause = (reason: string) => {
      stopTimer()
      notifiedRunIds.clear()
      patchNotifyState({ paused: true, reason })
    }

    // `since` is sent on every request; offset only advances within one `since`
    // window so several runs in the same second are all fetched before `since`
    // moves past them. A cancelled tick is discarded by returning null.
    const collect = (cursor: number) =>
      collectRunPages(
        (offset) => apiClient.getSchedulerRunsSince(cursor, PAGE_LIMIT, offset),
        () => cancelled,
      )

    async function tick() {
      if (cancelled) return
      const cursor = since
      let runs: SchedulerRun[] | null
      try {
        runs = await collect(cursor)
      } catch (err) {
        if (cancelled) return
        if (pollErrorDecision(err) === 'retry') {
          schedule()
        } else {
          const reason =
            err instanceof FeatureUnavailableError
              ? err.reason
              : err instanceof ContextError
                ? err.kind
                : 'error'
          console.warn('[scheduler-notify] polling stopped:', err)
          pause(reason)
        }
        return
      }
      if (cancelled || runs === null) return
      // Oldest first so `since` advances monotonically and notifications arrive
      // in execution order; run_id is the tiebreaker for same-second runs.
      const batch = planNotifyBatch(runs, since, notifiedRunIds)
      since = batch.since
      // Always force a notification, even for the currently-open session and
      // for manual "run now": a scheduled execution should announce itself
      // wherever the user is. usePushPoll deliberately skips notifying for
      // scheduler pushes so this is the single source of those notifications.
      for (const run of batch.runs) {
        notifyScheduledRun(run.session_id, run.task_name || '', run.output_preview || '')
      }
      schedule()
    }

    tick()
    return () => {
      cancelled = true
      loopActive = false
      stopTimer()
      // Pausing/leaving clears the seen set, so a later identity never inherits
      // dedupe state (and the restart cannot resurrect old notifications).
      notifiedRunIds.clear()
      patchNotifyState({ available: false, paused: false, reason: '' })
    }
  }, [allowed, epoch, tenantId, revision])
}
