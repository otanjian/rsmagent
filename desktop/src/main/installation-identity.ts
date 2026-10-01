/**
 * The installation identity the desktop reports to the server.
 *
 * Change ``align-desktop-project-execution-with-master`` (task 2.2). The id
 * identifies one installation so a device row and a local grant can be scoped
 * to it; it is **never** an authorization -- server-side membership, pairing and
 * the per-call checks decide what a device may do.
 *
 * The value lives in the main process, not in page storage:
 *
 *   * it survives a page reload, a cache clear and a reinstall of the web
 *     assets, so "the same machine" keeps one device row;
 *   * a page cannot mint it, so a compromised document cannot point a
 *     confirmation at another installation;
 *   * the old page-side value is adopted once, so an upgrade does not orphan the
 *     device already registered from it.
 *
 * Pure Node (paths and bytes injected) so the create / repair / migrate paths
 * are exercised without Electron.
 */

import { randomBytes } from 'crypto'
import { existsSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'fs'
import * as path from 'path'

/** 24 random bytes, base64url: the same floor the server-side ids use. */
const ID_PATTERN = /^[A-Za-z0-9_-]{22,128}$/

export interface InstallationIdentity {
  /** The stable id. Never a bearer token. */
  id: string
  /** True when no id existed and one was created. */
  created: boolean
  /** True when a stored id was unusable and had to be replaced. */
  repaired: boolean
  /** True when a page-supplied id was adopted instead of generating a new one. */
  adopted: boolean
}

export interface ReadIdentityOptions {
  /** Absolute path of the identity file (``<userData>/installation.json``). */
  file: string
  /**
   * A page-supplied candidate from before the id moved into the main process.
   * Only used on the very first run (no file yet); never after.
   */
  legacyId?: string
  now?: () => number
}

/** A fresh opaque id. */
export function newInstallationId(): string {
  return randomBytes(24).toString('base64url')
}

/** Whether an id is well formed and may be reused as-is. */
export function isValidInstallationId(value: unknown): value is string {
  return typeof value === 'string' && ID_PATTERN.test(value)
}

function parseStoredId(raw: string): string {
  try {
    const parsed = JSON.parse(raw) as { id?: unknown } | null
    return parsed && typeof parsed.id === 'string' ? parsed.id : ''
  } catch {
    return ''
  }
}

/** Write atomically: a crash must not leave a half-written, unreadable file. */
export function writeInstallationId(file: string, id: string, now: () => number = Date.now): void {
  if (!isValidInstallationId(id)) throw new Error('invalid installation id')
  mkdirSync(path.dirname(file), { recursive: true })
  const tmp = `${file}.${process.pid}.tmp`
  try {
    writeFileSync(tmp, JSON.stringify({ id, createdAt: now() }), { mode: 0o600 })
    renameSync(tmp, file)
  } catch (err) {
    try { unlinkSync(tmp) } catch { /* the temp file may not exist */ }
    throw err
  }
}

/**
 * Read the installation id, creating or repairing it when necessary.
 *
 * - a valid stored id is returned unchanged;
 * - a corrupt or missing id is replaced (``repaired`` / ``created``), never
 *   "fixed" by trusting whatever the file contained;
 * - a legacy page-supplied id is adopted only when there is no file yet, so the
 *   device already registered from it is not orphaned by the upgrade.
 */
export function loadOrCreateInstallationId(options: ReadIdentityOptions): InstallationIdentity {
  const { file, legacyId } = options
  const exists = existsSync(file)
  if (exists) {
    const stored = parseStoredId(readFileSync(file, 'utf8'))
    if (isValidInstallationId(stored)) {
      return { id: stored, created: false, repaired: false, adopted: false }
    }
  }
  // No usable id. Prefer the caller's legacy value on a first run so an upgrade
  // keeps the existing device association; otherwise mint one.
  const adopt = !exists && isValidInstallationId(legacyId)
  const id = adopt ? (legacyId as string) : newInstallationId()
  writeInstallationId(file, id, options.now)
  return { id, created: !exists && !adopt, repaired: exists, adopted: adopt }
}

/** The identity file for a desktop user-data directory. */
export function installationIdentityPath(userDataDir: string): string {
  return path.join(userDataDir, 'installation.json')
}

const cache = new Map<string, InstallationIdentity>()

/**
 * Memoised per user-data directory.
 *
 * ``legacyId`` only matters the first time; later calls just return the cached
 * answer, so a page cannot swap the identity on a subsequent confirmation.
 */
export function installationIdentityFor(userDataDir: string, legacyId?: string): InstallationIdentity {
  const cached = cache.get(userDataDir)
  if (cached) return cached
  const resolved = loadOrCreateInstallationId({
    file: installationIdentityPath(userDataDir),
    legacyId,
  })
  cache.set(userDataDir, resolved)
  return resolved
}

/** Test / account-switch helper: forget the memoised identity. */
export function forgetInstallationIdentity(userDataDir?: string): void {
  if (userDataDir) cache.delete(userDataDir)
  else cache.clear()
}
