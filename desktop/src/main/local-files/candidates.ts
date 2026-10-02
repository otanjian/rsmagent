/**
 * Persisted directory *candidates* — not active grants.
 *
 * Change ``add-desktop-remote-web-workbench`` (task 8.5). Absolute paths live
 * only on the local machine, in a file the OS user can read and nobody else
 * should. They never sync to the server, never enter telemetry, and never
 * become an active grant on their own: a restart shows the candidate list and
 * asks the user to reconnect (D6).
 *
 * Pure Node so the on-disk shape and the "candidate ≠ grant" rule can be
 * tested without Electron.
 */

import * as fs from 'fs'
import * as path from 'path'
import * as crypto from 'crypto'

export const CANDIDATES_VERSION = 1

export interface DirectoryCandidate {
  id: string
  /** Absolute path — local only. */
  absolutePath: string
  label: string
  serverId: string
  userId: string
  tenantId: string
  deviceId: string
  lastUsedAt: number
}

export interface CandidatesFile {
  version: number
  candidates: DirectoryCandidate[]
}

function emptyFile(): CandidatesFile {
  return { version: CANDIDATES_VERSION, candidates: [] }
}

function newId(): string {
  return crypto.randomBytes(16).toString('base64url')
}

/**
 * Load candidates from ``filePath``. A missing or corrupt file is an empty
 * list, never a throw that would block startup — the user can always pick
 * again.
 */
export function loadCandidates(filePath: string): CandidatesFile {
  try {
    const raw = fs.readFileSync(filePath, 'utf8')
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || parsed.version !== CANDIDATES_VERSION) {
      return emptyFile()
    }
    if (!Array.isArray(parsed.candidates)) return emptyFile()
    const candidates: DirectoryCandidate[] = []
    for (const row of parsed.candidates) {
      if (!row || typeof row !== 'object') continue
      if (typeof row.absolutePath !== 'string' || !row.absolutePath) continue
      if (typeof row.label !== 'string' || !row.label) continue
      candidates.push({
        id: typeof row.id === 'string' ? row.id : newId(),
        absolutePath: row.absolutePath,
        label: row.label,
        serverId: String(row.serverId || ''),
        userId: String(row.userId || ''),
        tenantId: String(row.tenantId || ''),
        deviceId: String(row.deviceId || ''),
        lastUsedAt: Number(row.lastUsedAt) || 0,
      })
    }
    return { version: CANDIDATES_VERSION, candidates }
  } catch {
    return emptyFile()
  }
}

/**
 * Persist candidates. The file is written with mode ``0o600`` so only the
 * current OS user can read the absolute paths (task 8.5).
 */
export function saveCandidates(filePath: string, file: CandidatesFile): void {
  const dir = path.dirname(filePath)
  fs.mkdirSync(dir, { recursive: true })
  const tmp = filePath + '.tmp-' + process.pid
  const body = JSON.stringify(
    { version: CANDIDATES_VERSION, candidates: file.candidates },
    null,
    2,
  )
  fs.writeFileSync(tmp, body, { encoding: 'utf8', mode: 0o600 })
  fs.renameSync(tmp, filePath)
  try {
    fs.chmodSync(filePath, 0o600)
  } catch {
    // Windows may not honor chmod; the important half is that we never widen.
  }
}

/** Upsert a candidate under the given scope. Absolute path is the identity. */
export function rememberCandidate(
  file: CandidatesFile,
  candidate: Omit<DirectoryCandidate, 'id' | 'lastUsedAt'> & { id?: string },
): CandidatesFile {
  const next = file.candidates.filter(
    (row) =>
      !(
        row.absolutePath === candidate.absolutePath &&
        row.serverId === candidate.serverId &&
        row.userId === candidate.userId &&
        row.tenantId === candidate.tenantId &&
        row.deviceId === candidate.deviceId
      ),
  )
  next.push({
    id: candidate.id || newId(),
    absolutePath: candidate.absolutePath,
    label: candidate.label,
    serverId: candidate.serverId,
    userId: candidate.userId,
    tenantId: candidate.tenantId,
    deviceId: candidate.deviceId,
    lastUsedAt: Date.now(),
  })
  // Bound the file: a user who picks many directories must not grow it forever.
  next.sort((a, b) => b.lastUsedAt - a.lastUsedAt)
  return { version: CANDIDATES_VERSION, candidates: next.slice(0, 50) }
}

/** Candidates visible for one scope. Absolute paths stay in the result — the
 * main process uses them to re-prompt; they are never sent to the server. */
export function candidatesForScope(
  file: CandidatesFile,
  scope: { serverId: string; userId: string; tenantId: string; deviceId: string },
): DirectoryCandidate[] {
  return file.candidates.filter(
    (row) =>
      row.serverId === scope.serverId &&
      row.userId === scope.userId &&
      row.tenantId === scope.tenantId &&
      row.deviceId === scope.deviceId,
  )
}

/** Drop every candidate for a user (account deletion / full reset). */
export function forgetUser(file: CandidatesFile, userId: string): CandidatesFile {
  return {
    version: CANDIDATES_VERSION,
    candidates: file.candidates.filter((row) => row.userId !== userId),
  }
}
