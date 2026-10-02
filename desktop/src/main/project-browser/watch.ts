// A *bounded* watcher over a bound local project (change
// align-desktop-project-execution-with-master, task 9.3).
//
// The panel must notice when the directory it is showing changes (an agent run
// wrote a report, the user edited a file in their own editor), and it must stop
// noticing the moment the authorization behind that directory goes away. Both
// halves are the point: a watcher that keeps reporting "nothing changed" for a
// directory the user has since re-picked, or that keeps a stale path in memory
// after a revoke, is worse than no watcher at all.
//
// Deliberately *not* `fs.watch`: the panel's reads go through the guarded helper
// (a grant, a realpath containment check, a page bound), and a watcher that
// opened the directory itself would be a second filesystem stack with its own
// idea of what the project is. Polling the same `tree` call the panel uses keeps
// one authority -- and one set of clamps -- in the process.
//
// Bounded on every axis: interval, directories, entries and depth per scan, one
// scan in flight, exponential backoff on failure, and no removals at all from a
// scan that hit a bound (a truncated scan has not looked everywhere, so "missing"
// would be a guess).

/** One scan never runs more often than this. */
export const WATCH_MIN_INTERVAL_MS = 1000

/** ... nor more slowly than this, backoff included. */
export const WATCH_MAX_INTERVAL_MS = 60000

/** What a healthy project polls at. Local I/O, and the panel is on screen. */
export const WATCH_DEFAULT_INTERVAL_MS = 2000

/** Directories visited per scan, including the root. */
export const WATCH_MAX_DIRECTORIES = 64

/** Entries read per scan, across all visited directories. */
export const WATCH_MAX_ENTRIES = 4000

/** How deep below the root the scan looks. */
export const WATCH_MAX_DEPTH = 4

/**
 * The largest page the device will answer.
 *
 * ``LIST_LIMIT_MAX`` in ``device-ops`` clamps to this, and the helper refuses a
 * larger ``page_size`` outright, so the watcher asks for what it will get
 * instead of trading a refusal for a bound it never needed.
 */
export const WATCH_PAGE_MAX = 200

/** The live authorization a watch is running under. */
export interface WatchScope {
  workspaceId: string
  bindingId: string
  selectionGeneration: number
  grantVersion: number
  connectionEpoch: string
}

/** What the page is told when something under the project changed. */
export interface ProjectChangedEvent {
  type: 'projectChanged'
  workspace_id: string
  binding_id: string
  selection_generation: number
  /** Project-relative directories whose listing changed (``''`` is the root). */
  changed: string[]
  /** Directories that are gone. Empty from a truncated scan. */
  removed: string[]
  /** A bound was hit: there may be more changes than this event names. */
  truncated: boolean
  scanned: number
}

/** What the page is told when watching ends, and why. */
export interface ProjectWatchEndedEvent {
  type: 'projectWatchEnded'
  workspace_id: string
  /**
   * ``stale_context``: the binding is gone or no longer the one this watch was
   * started under. The page must re-verify before it shows anything else.
   * ``stopped``: the panel is no longer showing this project.
   */
  reason: 'stale_context' | 'stopped'
}

export type ProjectWatchEvent = ProjectChangedEvent | ProjectWatchEndedEvent

/** Just the part of ``ProjectBrowser`` a scan needs. */
export interface ProjectTreeReader {
  tree(params: Record<string, unknown>): Promise<unknown>
}

export interface ProjectWatcherOptions {
  /** The panel's own reader: one clamp table, one journal, one project. */
  reader: ProjectTreeReader
  /** The live authorization for a workspace, or ``null`` when there is none. */
  scope: (workspaceId: string) => WatchScope | null
  /** Where events go (the host event channel). */
  emit: (event: ProjectWatchEvent) => void
  intervalMs?: number
  maxDirectories?: number
  maxEntries?: number
  maxDepth?: number
  schedule?: (run: () => void, ms: number) => unknown
  cancel?: (handle: unknown) => void
}

export interface ProjectWatcherStatus {
  watching: boolean
  workspace_id: string
  scans: number
  changes: number
  interval_ms: number
  last_error: string
  /** The last scan hit a bound, so its answer is partial by construction. */
  truncated: boolean
}

export class ProjectWatcher {
  private workspaceId = ''
  private scope: WatchScope | null = null
  /** ``path -> fingerprint`` of the last *complete* look at that directory. */
  private seen = new Map<string, string>()
  private timer: unknown = null
  private inFlight = false
  private stopped = true
  private failures = 0
  private baseInterval: number
  private interval: number
  private scans = 0
  private changes = 0
  private lastError = ''
  private lastTruncated = false

  private readonly schedule: (run: () => void, ms: number) => unknown
  private readonly cancel: (handle: unknown) => void

  constructor(private readonly options: ProjectWatcherOptions) {
    this.baseInterval = clampInterval(options.intervalMs ?? WATCH_DEFAULT_INTERVAL_MS)
    this.interval = this.baseInterval
    this.schedule = options.schedule ?? ((run, ms) => setTimeout(run, ms))
    this.cancel = options.cancel ?? ((handle) => clearTimeout(handle as NodeJS.Timeout))
  }

  status(): ProjectWatcherStatus {
    return {
      watching: !this.stopped && !!this.workspaceId,
      workspace_id: this.workspaceId,
      scans: this.scans,
      changes: this.changes,
      interval_ms: this.interval,
      last_error: this.lastError,
      truncated: this.lastTruncated,
    }
  }

  /**
   * Watch one workspace, replacing any previous watch.
   *
   * The first tick is a *baseline*: it records what the project looks like and
   * reports nothing. Reporting on it would tell the page that every directory
   * just changed, which is exactly the false positive that trains a user to
   * ignore the feature.
   */
  start(workspaceId: string): void {
    const target = String(workspaceId || '')
    if (!target) return
    if (!this.stopped && this.workspaceId === target) return
    if (!this.stopped) this.stop('stopped')
    this.workspaceId = target
    this.stopped = false
    this.failures = 0
    this.interval = this.baseInterval
    this.lastError = ''
    this.lastTruncated = false
    // A fresh scope: nothing about the previous project's shapes may survive
    // into this one, or its "new" files would compare against another
    // directory's listing.
    this.seen.clear()
    this.scope = null
    this.arm(0)
  }

  /**
   * Stop watching and forget the project.
   *
   * ``seen`` is cleared rather than kept for a later restart: an authorization
   * that was revoked must leave nothing behind that a future watch could mistake
   * for its own baseline.
   */
  stop(reason: 'stale_context' | 'stopped' = 'stopped'): void {
    const wasWatching = !this.stopped
    const workspaceId = this.workspaceId
    if (this.timer !== null) {
      this.cancel(this.timer)
      this.timer = null
    }
    this.stopped = true
    this.workspaceId = ''
    this.scope = null
    this.seen.clear()
    this.failures = 0
    this.interval = this.baseInterval
    if (wasWatching && workspaceId) {
      this.options.emit({ type: 'projectWatchEnded', workspace_id: workspaceId, reason })
    }
  }

  /** Re-read the authorization now, stopping if it is no longer the watched one. */
  verifyScope(): boolean {
    if (this.stopped || !this.workspaceId) return false
    if (this.readScope() === null) {
      this.stop('stale_context')
      return false
    }
    return true
  }

  /**
   * One bounded scan. Returns the change event it emitted, or ``null``.
   *
   * Public so a test can drive the clock instead of waiting for one: nothing
   * here depends on the timer, and the timer only decides *when* this runs.
   */
  async tick(): Promise<ProjectChangedEvent | null> {
    if (this.stopped || !this.workspaceId) return null
    // One scan at a time. A project slow enough for a scan to overlap the next
    // interval would otherwise queue scans and read the same directories twice.
    if (this.inFlight) return null
    this.inFlight = true
    try {
      return await this.scanOnce()
    } finally {
      this.inFlight = false
      if (!this.stopped) this.arm(this.nextDelay())
    }
  }

  private async scanOnce(): Promise<ProjectChangedEvent | null> {
    const scope = this.readScope()
    if (!scope) {
      this.stop('stale_context')
      return null
    }
    this.scope = scope
    this.scans += 1

    const scan = await this.scan()
    if (!scan.ok) {
      // The scope is not the problem (a revoke is caught above and by the next
      // tick): back off and say nothing. A change event here would be invented.
      this.failures += 1
      this.lastError = scan.code
      this.lastTruncated = false
      return null
    }
    this.failures = 0
    this.lastError = ''
    this.lastTruncated = scan.truncated

    // The first scan only records shapes: everything differs from an empty
    // baseline, and reporting that would say "the whole project changed" the
    // moment the panel opened.
    if (this.seen.size === 0) {
      this.seen = scan.seen
      return null
    }
    const event = this.diff(scan)
    this.seen = scan.seen
    if (event === null) return null
    this.changes += 1
    this.options.emit(event)
    return event
  }

  /** The live authorization, or ``null`` when it is gone or has moved on. */
  private readScope(): WatchScope | null {
    let current: WatchScope | null = null
    try {
      current = this.options.scope(this.workspaceId)
    } catch {
      return null
    }
    if (!current || current.workspaceId !== this.workspaceId) return null
    // Any of these moving means the *authorization* moved (a re-pick, a
    // re-attach, a new grant version): the watch was about a directory the user
    // no longer has open, so it stops rather than following the new one. The
    // page decides what to show next, after re-verifying.
    if (this.scope && scopeKey(current) !== scopeKey(this.scope)) return null
    return current
  }

  /** Breadth-first, bounded, and honest about its own truncation. */
  private async scan(): Promise<
    | { ok: true; seen: Map<string, string>; truncated: boolean }
    | { ok: false; code: string }
  > {
    const maxDirectories = this.options.maxDirectories ?? WATCH_MAX_DIRECTORIES
    const maxEntries = this.options.maxEntries ?? WATCH_MAX_ENTRIES
    const maxDepth = this.options.maxDepth ?? WATCH_MAX_DEPTH
    const pageMax = Math.min(maxEntries, WATCH_PAGE_MAX)
    const queue: Array<{ path: string; depth: number }> = [{ path: '', depth: 0 }]
    const seen = new Map<string, string>()
    let entries = 0
    let truncated = false
    while (queue.length > 0) {
      // Out of budget with directories still queued. The loop can only be left
      // this way -- the other exit is an empty queue -- so ``queue.length`` below
      // is the single statement of "this scan is partial"; saying it twice is how
      // the two statements drift apart.
      if (seen.size >= maxDirectories || entries >= maxEntries) break
      const dir = queue.shift()!
      const pageSize = Math.min(pageMax, Math.max(1, maxEntries - entries))
      let reply: Record<string, unknown>
      try {
        reply = (await this.options.reader.tree({
          workspace_id: this.workspaceId,
          path: dir.path,
          limit: pageSize,
        })) as Record<string, unknown>
      } catch (err) {
        return { ok: false, code: (err as { code?: string })?.code || 'device_error' }
      }
      if (!reply || reply.ok !== true) {
        return { ok: false, code: String((reply && reply.code) || 'device_error') }
      }
      const rows = Array.isArray(reply.entries) ? (reply.entries as Array<Record<string, unknown>>) : []
      entries += rows.length
      // Two different "there is more" signals, and neither alone is enough:
      // `truncated` is the helper's own scan budget (a directory with an
      // enormous number of names), while a *full* page that carries a cursor
      // means the rest of this directory is on a page the scan did not read.
      // Either one makes what was seen a partial view -- which must never be
      // read as a deletion -- so either one counts as truncation.
      if (reply.truncated) truncated = true
      if (rows.length >= pageSize && reply.next_cursor) truncated = true
      seen.set(dir.path, fingerprint(rows))
      if (dir.depth >= maxDepth) {
        if (rows.some((row) => String(row.kind || '') === 'dir')) truncated = true
        continue
      }
      for (const row of rows) {
        if (String(row.kind || '') !== 'dir') continue
        const path = String(row.path || '')
        if (!path) continue
        queue.push({ path, depth: dir.depth + 1 })
      }
    }
    if (queue.length > 0) truncated = true
    return { ok: true, seen, truncated }
  }

  /** What changed between the last complete look and this scan, or ``null``. */
  private diff(scan: { seen: Map<string, string>; truncated: boolean }): ProjectChangedEvent | null {
    const changed: string[] = []
    for (const [path, shape] of scan.seen) {
      const before = this.seen.get(path)
      if (before !== shape) changed.push(path)
    }
    // A truncated scan has not looked everywhere, so a directory it did not
    // reach is *unvisited*, not gone. Reporting it as removed would tell the
    // page that half the project was deleted.
    const removed: string[] = []
    if (!scan.truncated) {
      for (const path of this.seen.keys()) {
        if (!scan.seen.has(path)) removed.push(path)
      }
    }
    if (changed.length === 0 && removed.length === 0) return null
    return {
      type: 'projectChanged',
      workspace_id: this.workspaceId,
      binding_id: String(this.scope?.bindingId || ''),
      selection_generation: Number(this.scope?.selectionGeneration || 0),
      changed: changed.sort(),
      removed: removed.sort(),
      truncated: scan.truncated,
      scanned: scan.seen.size,
    }
  }

  private nextDelay(): number {
    if (this.failures > 0) {
      const grown = this.baseInterval * Math.pow(2, Math.min(this.failures, 3))
      this.interval = clampInterval(grown)
      return this.interval
    }
    // A project big enough to hit the scan bounds is polled at the slow end:
    // the cost of a scan is proportional to what it read, not to the interval.
    this.interval = this.lastTruncated ? WATCH_MAX_INTERVAL_MS : this.baseInterval
    return this.interval
  }

  private arm(ms: number): void {
    if (this.timer !== null) {
      this.cancel(this.timer)
      this.timer = null
    }
    this.timer = this.schedule(() => {
      this.timer = null
      void this.tick()
    }, ms)
  }
}

/** Transport reconnects change epoch, not the directory's authorization.
 * Device command execution still checks its own epoch; local read-only watches
 * must survive the initial handshake and subsequent reconnects.
 */
function scopeKey(scope: WatchScope): string {
  return [
    scope.workspaceId,
    scope.bindingId,
    scope.selectionGeneration,
    scope.grantVersion,
  ].join('\u0000')
}

/**
 * A cheap shape of one directory listing.
 *
 * Names, count, sizes and the newest timestamp: enough to notice a file added,
 * removed, renamed or rewritten, without reading a single byte of content (a
 * content hash per tick is not bounded I/O, and the panel's own save path
 * re-checks the mtime it loaded). The helper reports whole seconds, so two
 * writes inside one second with the same total size are below this resolution --
 * documented rather than papered over.
 */
function fingerprint(rows: Array<Record<string, unknown>>): string {
  const names: string[] = []
  let bytes = 0
  let newest = 0
  for (const row of rows) {
    names.push(`${String(row.name || '')}\u0001${String(row.kind || '')}`)
    bytes += Number(row.size || 0)
    newest = Math.max(newest, Number(row.modified || 0))
  }
  names.sort()
  return `${rows.length}:${bytes}:${newest}:${hash(names.join('\u0000'))}`
}

/** FNV-1a over the joined names: stable, tiny, and not a security boundary. */
function hash(text: string): string {
  let value = 0x811c9dc5
  for (let i = 0; i < text.length; i += 1) {
    value ^= text.charCodeAt(i)
    value = Math.imul(value, 0x01000193) >>> 0
  }
  return value.toString(16)
}

function clampInterval(ms: number): number {
  if (!Number.isFinite(ms)) return WATCH_DEFAULT_INTERVAL_MS
  return Math.min(WATCH_MAX_INTERVAL_MS, Math.max(WATCH_MIN_INTERVAL_MS, Math.floor(ms)))
}
