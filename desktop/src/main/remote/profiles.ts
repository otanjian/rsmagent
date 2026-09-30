// Desktop remote-server profiles: the versioned, validated local config.
//
// Change ``add-desktop-remote-web-workbench`` (tasks 2.1/2.2). The desktop can
// run in two modes: the existing ``local`` mode (bundled React UI + the local
// Python backend) and the new ``remote`` mode (an isolated container carrying
// the server's own Web workbench). ``remote`` must never start the backend and
// ``local`` must never be silently converted, so the mode and the servers the
// user added live here, in one versioned file, behind validation.
//
// What it stores, and what it refuses
// -----------------------------------
// Only: the mode, exact HTTPS origins (plus a display name) and non-secret
// preferences. Never a token, cookie, password, username or absolute local
// path -- the native session stays in the main process's memory
// (``auth-broker.ts``) and is re-authorized on every launch.
//
// A server origin is rejected unless it is an exact HTTPS origin with no
// userinfo, query, fragment or path prefix: a URL that carries ``user:pass``
// would leak into logs, and a prefix path is unsupported by V1 (contracts §1).
// HTTP public origins are refused outright; the only permitted local HTTP is
// the in-process backend origin, which is never a *stored* profile.
//
// The file is a pure Node module (no Electron import) so its validation and
// migration can be exercised directly by ``tests/test_desktop_profiles.cjs``.

import * as fs from 'fs'
import * as path from 'path'

/** The two run modes. ``local`` is the default for every existing install. */
export type DesktopMode = 'local' | 'remote'

/** The current config schema version. Bump only with a migration below. */
export const CONFIG_VERSION = 1

export interface ServerProfile {
  /** Local, opaque id; never sent to a server as an identifier. */
  id: string
  /** Exact ``https://host[:port]`` origin, normalized, no trailing slash. */
  origin: string
  /** User-facing label. Cosmetic only; never used for authorization. */
  displayName: string
}

export interface DesktopConfig {
  version: number
  mode: DesktopMode
  profiles: ServerProfile[]
  /** The one active remote server, or null. At most one, ever. */
  activeProfileId: string | null
  /** Non-secret UI preferences only. */
  preferences: Record<string, unknown>
}

export type OriginResult =
  | { ok: true; origin: string }
  | { ok: false; reason: string }

/** Schemes that may never become a stored remote server. */
const FORBIDDEN_SCHEMES = new Set(['http:', 'ftp:', 'file:', 'ws:', 'wss:', 'javascript:'])

/**
 * Validate and normalize a user-entered server address to an exact HTTPS origin.
 *
 * Returns a reason code on refusal (never throws) so the settings shell can show
 * the specific problem instead of a generic failure. The reason codes are stable
 * strings the UI maps to localized copy.
 */
export function parseServerOrigin(raw: unknown): OriginResult {
  if (typeof raw !== 'string' || !raw.trim()) {
    return { ok: false, reason: 'empty' }
  }
  const text = raw.trim()
  let url: URL
  try {
    url = new URL(text)
  } catch {
    return { ok: false, reason: 'invalid_url' }
  }
  if (FORBIDDEN_SCHEMES.has(url.protocol)) {
    return { ok: false, reason: 'scheme_not_https' }
  }
  if (url.protocol !== 'https:') {
    return { ok: false, reason: 'scheme_not_https' }
  }
  // ``user:pass@host`` would be logged and would not be an origin. Never store it.
  if (url.username || url.password) {
    return { ok: false, reason: 'has_userinfo' }
  }
  if (url.search || url.hash) {
    return { ok: false, reason: 'has_query_or_fragment' }
  }
  // V1 supports origin-root deployment only; a path prefix is refused rather
  // than silently stripped (a stripped prefix would load the wrong app).
  if (url.pathname && url.pathname !== '/') {
    return { ok: false, reason: 'has_path_prefix' }
  }
  const host = url.hostname
  if (!host) {
    return { ok: false, reason: 'invalid_url' }
  }
  const origin = url.port ? `https://${host}:${url.port}` : `https://${host}`
  return { ok: true, origin }
}

/** A fresh, empty config: local mode, nothing configured. */
export function defaultConfig(): DesktopConfig {
  return { version: CONFIG_VERSION, mode: 'local', profiles: [], activeProfileId: null, preferences: {} }
}

/**
 * Coerce arbitrary parsed JSON into a valid config, or report why it cannot be.
 *
 * A config written by a *newer* version is refused outright (``ok:false``)
 * instead of partially interpreted: dropping unknown fields could silently
 * discard a future security setting. Everything else is repaired into shape,
 * because a corrupt preferences blob must not strand the app.
 */
export function normalizeConfig(raw: unknown): { ok: true; config: DesktopConfig } | { ok: false; reason: string } {
  if (raw === null || raw === undefined) return { ok: true, config: defaultConfig() }
  if (typeof raw !== 'object' || Array.isArray(raw)) return { ok: false, reason: 'not_an_object' }
  const obj = raw as Record<string, unknown>

  const version = typeof obj.version === 'number' ? obj.version : 0
  if (version > CONFIG_VERSION) return { ok: false, reason: 'newer_version' }

  const migrated = version < CONFIG_VERSION ? migrate(obj, version) : obj

  const mode: DesktopMode = migrated.mode === 'remote' ? 'remote' : 'local'

  const profiles: ServerProfile[] = []
  const seen = new Set<string>()
  const rawProfiles = Array.isArray(migrated.profiles) ? migrated.profiles : []
  for (const entry of rawProfiles) {
    if (!entry || typeof entry !== 'object') continue
    const item = entry as Record<string, unknown>
    const parsed = parseServerOrigin(item.origin)
    if (!parsed.ok) continue
    if (seen.has(parsed.origin)) continue
    seen.add(parsed.origin)
    const id = typeof item.id === 'string' && item.id ? item.id : `srv_${profiles.length + 1}`
    const displayName = typeof item.displayName === 'string' && item.displayName.trim()
      ? item.displayName.trim()
      : parsed.origin.replace(/^https:\/\//, '')
    profiles.push({ id, origin: parsed.origin, displayName })
  }

  // At most one active remote server; an id with no profile is dropped (the
  // user may have removed the server in another window).
  const activeRaw = typeof migrated.activeProfileId === 'string' ? migrated.activeProfileId : null
  const activeProfileId = activeRaw && profiles.some((p) => p.id === activeRaw) ? activeRaw : null

  const preferences = migrated.preferences && typeof migrated.preferences === 'object'
    && !Array.isArray(migrated.preferences)
    ? (migrated.preferences as Record<string, unknown>)
    : {}

  return {
    ok: true,
    config: { version: CONFIG_VERSION, mode, profiles, activeProfileId, preferences },
  }
}

/**
 * Bring a pre-versioned config forward.
 *
 * The only shape that ever shipped is a single ``serverUrl`` string with an
 * optional ``mode``; it becomes one profile plus the active pointer when the URL
 * is a valid origin, and is otherwise dropped. No user, token or path field is
 * carried forward -- there never was one, and inventing one here would be the
 * exact mistake this migration exists to avoid.
 */
function migrate(obj: Record<string, unknown>, version: number): Record<string, unknown> {
  if (version >= 1) return obj
  const out: Record<string, unknown> = {
    mode: obj.mode,
    profiles: obj.profiles,
    activeProfileId: obj.activeProfileId,
    preferences: obj.preferences,
  }
  if (typeof obj.serverUrl === 'string' && obj.serverUrl.trim()) {
    const parsed = parseServerOrigin(obj.serverUrl)
    if (parsed.ok) {
      out.profiles = [{ id: 'srv_1', origin: parsed.origin, displayName: parsed.origin.replace(/^https:\/\//, '') }]
      out.activeProfileId = 'srv_1'
    }
  }
  return out
}

/** Read the config file. A missing or unreadable file yields the default. */
export function loadConfig(file: string): { config: DesktopConfig; refused?: string } {
  let raw: unknown
  try {
    raw = JSON.parse(fs.readFileSync(file, 'utf-8'))
  } catch {
    return { config: defaultConfig() }
  }
  const result = normalizeConfig(raw)
  if (!result.ok) {
    // Refuse, and let the caller keep the old file on disk: overwriting a newer
    // config would destroy settings this build does not understand.
    return { config: defaultConfig(), refused: result.reason }
  }
  return { config: result.config }
}

/**
 * Write the config atomically with owner-only permissions.
 *
 * Temp file + rename so a crash cannot leave a half-written config, and mode
 * ``0o600`` because the file may hold non-secret but still private preferences.
 */
export function saveConfig(file: string, config: DesktopConfig): void {
  const dir = path.dirname(file)
  fs.mkdirSync(dir, { recursive: true })
  const tmp = `${file}.tmp-${process.pid}-${Date.now()}`
  fs.writeFileSync(tmp, JSON.stringify(config, null, 2), { encoding: 'utf-8', mode: 0o600 })
  fs.renameSync(tmp, file)
}

/** The active profile, or null when none is selected / set. */
export function activeProfile(config: DesktopConfig): ServerProfile | null {
  if (!config.activeProfileId) return null
  return config.profiles.find((p) => p.id === config.activeProfileId) || null
}

/**
 * Add or update a server from a raw address, without changing the mode.
 *
 * Returns the updated config, the profile id and whether it already existed. An
 * already-present origin reuses its id (so the same server is never duplicated).
 */
export function addServer(
  config: DesktopConfig,
  rawOrigin: unknown,
  displayName?: string,
): { ok: true; config: DesktopConfig; id: string; existed: boolean } | { ok: false; reason: string } {
  const parsed = parseServerOrigin(rawOrigin)
  if (!parsed.ok) return { ok: false, reason: parsed.reason }
  const existing = config.profiles.find((p) => p.origin === parsed.origin)
  if (existing) {
    return { ok: true, config, id: existing.id, existed: true }
  }
  const id = `srv_${Date.now().toString(36)}`
  const profile: ServerProfile = {
    id,
    origin: parsed.origin,
    displayName: (displayName && displayName.trim()) || parsed.origin.replace(/^https:\/\//, ''),
  }
  return {
    ok: true,
    config: { ...config, profiles: [...config.profiles, profile] },
    id,
    existed: false,
  }
}
