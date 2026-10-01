/**
 * What this device last served for a file, so an edit can notice an outside change
 * (change ``align-desktop-project-execution-with-master``, task 7.4 / A20).
 *
 * The master already tracks this in ``agent/tools/utils/file_state.py`` -- but
 * that record keys on ``os.path.realpath`` of a path **on the master's** disk.
 * When the project lives on a device, the master's stat answers about the wrong
 * machine: the file does not exist there, ``getmtime`` fails, and the staleness
 * check silently disappears exactly where two writers are most likely to
 * collide. So the check has to be made where the bytes are.
 *
 * The rule is the master's rule, not a stricter one: an outside change is
 * *reported*, not blocked. The agent runs across channels where the user editing
 * a file by hand is normal, and refusing the edit would strand the model with no
 * way forward; the contract's own ``file_changed`` claim is ``partial``, which is
 * exactly "applied, but on top of a view that moved".
 *
 * Pure Node, with the ``stat`` and the stamp values injected, so the boundary
 * cases (a file that vanished, a clock that cannot be trusted) are exercised
 * without depending on the filesystem's timestamp resolution.
 */

import { statSync } from 'node:fs'

/** What identifies one version of a file, cheaply enough to stat on every run. */
export interface FileStamp {
  mtimeMs: number
  size: number
}

export interface FileVersionsOptions {
  /** ``null`` when the file does not exist or cannot be stat-ed. */
  stat?: (path: string) => FileStamp | null
  /** Bounded so a long session cannot grow without limit. */
  maxTracked?: number
}

const DEFAULT_MAX_TRACKED = 512

export class FileVersions {
  private readonly stat: (path: string) => FileStamp | null
  private readonly maxTracked: number
  /** Insertion-ordered so the oldest entry is the one evicted. */
  private readonly seen = new Map<string, FileStamp>()

  constructor(options: FileVersionsOptions = {}) {
    this.stat = options.stat ?? defaultStat
    this.maxTracked = options.maxTracked ?? DEFAULT_MAX_TRACKED
  }

  /**
   * The device has just handed these contents to the model.
   *
   * A write is also a point at which the model's view becomes current, so
   * ``noteWritten`` is the same record under a different name: after our own
   * write, an edit is not stale with respect to anything.
   */
  noteServed(path: string): void {
    const stamp = this.stat(path)
    if (!stamp) return
    this.seen.delete(path)
    this.seen.set(path, stamp)
    while (this.seen.size > this.maxTracked) {
      const oldest = this.seen.keys().next()
      if (oldest.done) break
      this.seen.delete(oldest.value)
    }
  }

  /** Alias with the intent spelled out: our own write is a fresh view. */
  noteWritten(path: string): void {
    this.noteServed(path)
  }

  /** Forget one path (it was deleted, or a project root was detached). */
  forget(path: string): void {
    this.seen.delete(path)
  }

  /** Forget everything under a root: the workspace was unbound or revoked. */
  forgetRoot(prefix: string): void {
    for (const path of [...this.seen.keys()]) {
      if (path === prefix || path.startsWith(prefix.endsWith('/') ? prefix : `${prefix}/`)) {
        this.seen.delete(path)
      }
    }
  }

  /**
   * Why this frame's target is stale, or ``null`` when it is not.
   *
   * ``null`` also means "no expectation": a file the model never read has
   * nothing to be stale against, and warning on a first write would fire on
   * every legitimate one.
   */
  staleReason(path: string): string | null {
    const seen = this.seen.get(path)
    if (!seen) return null
    const current = this.stat(path)
    if (!current) {
      // It was there when we read it and is gone now. That is a change, and the
      // edit is about to recreate a file someone deliberately removed.
      return `was deleted after you last read it`
    }
    if (current.mtimeMs <= seen.mtimeMs && current.size === seen.size) return null
    return `was modified after you last read it`
  }

  /** The staleness reason for the first stale target, with its path. */
  firstStale(paths: readonly string[]): { path: string; reason: string } | null {
    for (const path of paths) {
      const reason = this.staleReason(path)
      if (reason) return { path, reason }
    }
    return null
  }

  /** How many files this device is currently holding a view of (diagnostics). */
  get tracked(): number {
    return this.seen.size
  }
}

function defaultStat(path: string): FileStamp | null {
  try {
    const info = statSync(path)
    if (!info.isFile()) return null
    return { mtimeMs: info.mtimeMs, size: info.size }
  } catch {
    return null
  }
}

/**
 * The warning a stale edit carries.
 *
 * Worded like the master's, because it is the same advice: the change *was*
 * applied, and the model should look again rather than assume.
 */
export function stalenessWarning(path: string, reason: string): string {
  const name = path.split('/').pop() || path
  return (
    `${name} ${reason} (by the user, a scheduled task, or another agent). `
    + 'Your change was applied on top of the older content - read the file '
    + 'again to confirm the result is what you intended.'
  )
}
