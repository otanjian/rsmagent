/**
 * Native actions on a file in a bound local project (task 9.4, A25).
 *
 * The panel can *show* a local file, but there are things only the system can
 * do: open it in whatever application the user has associated with it, show it
 * in the file manager, put its real path on the clipboard, or write a copy
 * somewhere else. Those four are one question -- "may this machine act on this
 * file?" -- and one answer, so they live in one module rather than in four
 * handlers that each re-derive the same containment check.
 *
 * Two properties are the whole point:
 *
 *   * **every action re-verifies.** Not "the panel saw this file five minutes
 *     ago": a live grant (the caller resolves the workspace through
 *     ``ProjectBrowser``, whose binding is re-read on every call), a helper
 *     ``stat`` that just answered about *this* path, a real path that is inside
 *     the project root, and an entry that is a file or a directory -- never a
 *     link. A symlink whose target is outside the project would otherwise be
 *     opened by the OS even though the helper refuses to read through it.
 *   * **the directory never goes back to the page.** The absolute path is
 *     computed and used *here* (the clipboard call is made in this process, not
 *     by the page) and the reply carries a file *name*. A page that could ask
 *     for a path would be a page that can read the user's home layout.
 *
 * Pure Node (no Electron import): the effects are injected, so a test drives
 * the whole flow -- including "the OS has no application for this file" -- with
 * real files and fake effects.
 */

import * as fs from 'fs'
import * as path from 'path'

/** The four actions, in the panel's order. */
export const NATIVE_ACTIONS = ['open', 'reveal', 'copyPath', 'saveAs'] as const

export type NativeAction = (typeof NATIVE_ACTIONS)[number]

/** The bridge method that asks for each action. */
export const NATIVE_METHOD_BY_ACTION: Record<NativeAction, string> = {
  open: 'projectOpenFile',
  reveal: 'projectRevealFile',
  copyPath: 'projectCopyPath',
  saveAs: 'projectSaveFileAs',
}

/**
 * The reverse: which action a bridge method means.
 *
 * The method *is* the action, so the bridge's allow-list and this table are the
 * only two places the vocabulary appears, and both refuse anything else.
 */
export const NATIVE_ACTION_BY_METHOD: Record<string, NativeAction> = {
  projectOpenFile: 'open',
  projectRevealFile: 'reveal',
  projectCopyPath: 'copyPath',
  projectSaveFileAs: 'saveAs',
}

export interface NativeActionPlan {
  ok: true
  kind: NativeAction
  /**
   * The absolute path of the verified file.
   *
   * It never leaves this process: it is the argument to the native call and the
   * thing the clipboard receives, not a field of any reply.
   */
  absolutePath: string
  /** The file's own name, which is what the user is told about. */
  name: string
  isDirectory: boolean
  /** The version the *helper* reported (whole seconds), as the panel saw it. */
  modified: number
  size: number
}

export interface NativeActionRefusal {
  ok: false
  code: string
  message: string
}

/** What the actions are carried out with. In the container these are Electron. */
export interface NativeActionEffects {
  /** Returns `''` on success, or the OS's own message. */
  openPath: (absolutePath: string) => Promise<string>
  revealPath: (absolutePath: string) => void
  /** Copies the path *on this machine*; the page never sees it. */
  copyPath: (absolutePath: string) => void
  /** The user's chosen destination, or null when the dialog was dismissed. */
  chooseDestination: (suggestedName: string) => Promise<string | null>
  copyFile: (source: string, destination: string) => Promise<void>
}

export type NativeActionResult =
  | {
      ok: true
      kind: NativeAction
      name: string
      is_directory: boolean
      /** `open` and `reveal` hand the file to the system, which did so. */
      opened?: boolean
      /** `copyPath`: the path is on this machine's clipboard. */
      copied?: boolean
      /** `saveAs`: a copy exists at a destination the user chose. */
      saved?: boolean
    }
  | NativeActionRefusal

/**
 * Whether this machine may act on one project-relative file, and where it is.
 *
 * ``root`` is the absolute directory of the live grant (``null`` when the grant
 * is gone), and ``entry`` is what ``ProjectBrowser.resolve`` just answered: a
 * refusal there means the helper did not confirm this path, so no action is
 * planned at all.
 */
export function planNativeAction(input: {
  kind: NativeAction
  path: unknown
  /** The grant's absolute root, or null. Main-process only. */
  root: string | null
  /** The helper's answer for this path. */
  entry: { ok?: boolean; code?: unknown; message?: unknown; kind?: unknown; size?: unknown; modified?: unknown }
}): NativeActionPlan | NativeActionRefusal {
  const kind = input.kind
  if (!(NATIVE_ACTIONS as readonly string[]).includes(kind)) {
    return refuse('invalid_request', 'this is not a native file action')
  }
  const entry = (input.entry || {}) as Record<string, unknown>
  if (entry.ok !== true) {
    // The helper's own refusal travels unchanged: `stale_context` is a revoked
    // grant, `not_found` is a file that is gone. Rewriting either into "cannot
    // open" would take away the one thing the user needs to know.
    return refuse(
      String(entry.code || '') || 'not_found',
      String(entry.message || '') || 'this file is not available on this machine',
    )
  }
  const relative = projectRelative(input.path)
  if (!relative) return refuse('invalid_request', 'a native action needs a project-relative path')
  const root = String(input.root || '')
  if (!root) return refuse('stale_context', 'the project this file belongs to is no longer authorized')
  const entryKind = String(entry.kind || '')
  if (entryKind !== 'file' && entryKind !== 'dir') {
    return refuse('not_a_file', `this entry is not a file or a directory (${entryKind || 'unknown'})`)
  }
  if (kind === 'saveAs' && entryKind === 'dir') {
    return refuse('not_a_file', 'a directory cannot be saved as one file')
  }

  let realRoot: string
  try {
    realRoot = fs.realpathSync(root)
  } catch {
    return refuse('stale_context', 'the project directory is no longer reachable')
  }
  const candidate = path.join(realRoot, ...relative.split('/'))
  let stat: fs.Stats
  try {
    stat = fs.lstatSync(candidate)
  } catch {
    return refuse('not_found', 'this file is no longer on disk')
  }
  // A link is not a file here: the helper classifies and refuses one, and the
  // OS would happily follow it out of the project.
  if (stat.isSymbolicLink()) {
    return refuse('not_a_file', 'this entry is a link, and the project does not follow links')
  }
  if (stat.isDirectory() !== (entryKind === 'dir')) {
    return refuse('changed', 'this entry changed on disk since the panel read it')
  }
  let real: string
  try {
    real = fs.realpathSync(candidate)
  } catch {
    return refuse('not_found', 'this file is no longer on disk')
  }
  // A directory component inside the project can still be a link out of it, so
  // containment is checked on the *resolved* path, not on the joined string.
  if (!isInside(realRoot, real)) {
    return refuse('unsafe_path', 'this entry resolves outside the project directory')
  }
  return {
    ok: true,
    kind,
    absolutePath: real,
    name: path.basename(real),
    isDirectory: stat.isDirectory(),
    // The helper's clock, not this process's: the caller compares it against
    // the version the panel saw, and one clock for both keeps that meaningful.
    modified: numberOr(entry.modified, 0),
    size: numberOr(entry.size, 0),
  }
}

/**
 * Carry out one planned action.
 *
 * A refusal is accepted as an input and handed straight back: the caller's
 * "plan, then perform" pair is then total, and a caller that forgets to check
 * gets the real reason instead of a crash in the middle of the effect.
 *
 * ``expectedMtime`` is the version the panel read before the user asked for a
 * copy. When it no longer matches, the copy is refused rather than silently
 * taken from a file the user has not seen -- the same rule the editor's save
 * path follows, and for the same reason. ``acceptCurrent`` is the user's
 * answer after being told.
 */
export async function performNativeAction(
  plan: NativeActionPlan | NativeActionRefusal,
  effects: NativeActionEffects,
  options: { expectedMtime?: number; acceptCurrent?: boolean } = {},
): Promise<NativeActionResult> {
  if (plan.ok !== true) {
    return refuse(String(plan.code || 'device_error'), String(plan.message || 'the action was not planned'))
  }
  switch (plan.kind) {
    case 'open': {
      let failure = ''
      try {
        failure = String(await effects.openPath(plan.absolutePath) || '')
      } catch (err) {
        failure = String((err as Error)?.message || err || 'the system could not open this file')
      }
      if (failure) return describeOpenFailure(failure)
      return {
        ok: true, kind: 'open', name: plan.name, is_directory: plan.isDirectory, opened: true,
      }
    }
    case 'reveal': {
      try {
        effects.revealPath(plan.absolutePath)
      } catch (err) {
        return refuse('device_error', String((err as Error)?.message || err || 'the file manager did not open'))
      }
      return {
        ok: true, kind: 'reveal', name: plan.name, is_directory: plan.isDirectory, opened: true,
      }
    }
    case 'copyPath': {
      // Done here, on this machine: the page asks for the *action* and is told
      // it happened. Handing the path back so the page could copy it would put
      // the user's directory layout in a document that does not need it.
      try {
        effects.copyPath(plan.absolutePath)
      } catch (err) {
        return refuse('device_error', String((err as Error)?.message || err || 'the path was not copied'))
      }
      return { ok: true, kind: 'copyPath', name: plan.name, is_directory: plan.isDirectory, copied: true }
    }
    default: {
      const expected = options.expectedMtime
      if (typeof expected === 'number' && expected > 0 && !options.acceptCurrent
        && expected !== plan.modified) {
        return refuse(
          'changed',
          `this file changed on disk since it was read (version ${expected} is now ${plan.modified})`,
        )
      }
      let destination: string | null = null
      try {
        destination = await effects.chooseDestination(plan.name)
      } catch (err) {
        return refuse('device_error', String((err as Error)?.message || err || 'no destination was chosen'))
      }
      if (!destination) return refuse('cancelled', 'no destination was chosen')
      if (path.resolve(String(destination)) === path.resolve(plan.absolutePath)) {
        // A copy onto itself is a truncate waiting to happen, and the name the
        // user picked says they meant to keep both.
        return refuse('invalid_request', 'the destination is the file itself; a copy needs another name')
      }
      try {
        await effects.copyFile(plan.absolutePath, String(destination))
      } catch (err) {
        return refuse('io_error', String((err as Error)?.message || err || 'the copy failed'))
      }
      return {
        ok: true,
        kind: 'saveAs',
        // The chosen *name* only: the directory is where the user just put it,
        // and it is not this process's to publish.
        name: path.basename(String(destination)),
        is_directory: false,
        saved: true,
      }
    }
  }
}

/**
 * The OS's own words for a failed open, as a code the panel can act on.
 *
 * ``shell.openPath`` answers with the platform's error text. A user who has no
 * application for a ``.numbers`` file needs to be told *that*, not "cannot
 * open"; and anything unrecognised is passed through verbatim rather than
 * flattened into a code that hides the real cause.
 */
export function describeOpenFailure(failure: unknown): NativeActionRefusal {
  const text = String(failure || '').trim()
  if (!text) return refuse('device_error', 'the system did not open this file')
  // Permission first: "Operation not permitted" is not a missing application,
  // and telling a user to install something they already have is worse than
  // saying nothing.
  if (/permission|denied|EACCES|EPERM|not permitted/i.test(text)) {
    return refuse('permission_denied', text)
  }
  if (/no such file|ENOENT|does not exist|not exist/i.test(text)) return refuse('not_found', text)
  // The platform's own wording for "nothing opens this". macOS says "The
  // application cannot be opened for an unexpected reason" and, when no default
  // is set, "There is no application set to open the document"; Linux says
  // "no application is associated with the specified file" or reports a
  // missing handler. Anything unrecognised is still passed through verbatim, so
  // a real message is never flattened into a code that hides it.
  if (/no application|application cannot be opened|application is associated|no handler|no.*registered|not associated|no default application|missing application/i.test(text)) {
    return refuse('no_application', text)
  }
  return refuse('device_error', text)
}

/** True when ``candidate`` is ``root`` itself or inside it. Both are resolved. */
function isInside(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate)
  return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative))
}

/**
 * A project-relative path, or ``''``.
 *
 * The shape rules are the bridge's as well; repeated here so the *planner*
 * cannot be handed an absolute path by any caller, including one that is not
 * the page (a future native caller, a test). Containment is still the realpath
 * check above -- this only refuses what could never be project-relative.
 */
function projectRelative(value: unknown): string {
  const raw = typeof value === 'string' ? value.trim().replace(/^\.\//, '') : ''
  if (!raw || raw.includes('\u0000')) return ''
  if (raw.startsWith('/') || /^[a-zA-Z]:/.test(raw)) return ''
  const parts = raw.split(/[\\/]+/)
  if (parts.some((part) => part === '..' || part === '' || part === '.')) return ''
  return parts.join('/')
}

function numberOr(value: unknown, fallback: number): number {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.trunc(value)
  if (typeof value === 'string' && /^-?\d+$/.test(value.trim())) return Number(value.trim())
  return fallback
}

function refuse(code: string, message: string): NativeActionRefusal {
  return { ok: false, code, message }
}
