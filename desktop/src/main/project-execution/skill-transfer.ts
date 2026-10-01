/**
 * Fetching a pinned skill version onto the device (task 8.9).
 *
 * Task 8.8 taught the *local* end to hand its sandbox read-only skill roots. The
 * remote end has the same need and one thing the local end does not: a device
 * that may not have the version at all. The command says which versions the run
 * is pinned to; this module is how the bytes for those versions get here.
 *
 * The shape is deliberately narrow and one-directional:
 *
 * * the device asks the **server that gave it the frame**, over the native
 *   credential it already uses for the broker endpoints, for one
 *   `(skill_id, digest)` pair at a time. It never takes a URL from the frame,
 *   and it never accepts a package the server volunteers;
 * * the request names the *command*, so the server can check the version against
 *   the set that command was authorized with (the row's own `skill_resources`)
 *   rather than against "a version that exists";
 * * the answer is verified locally before it is written: `SkillCache.install`
 *   recomputes the package digest and refuses a mismatch. A package that arrives
 *   wrong leaves nothing behind.
 *
 * The refusals are the interesting part. A transfer that fails is *not* a reason
 * to run something else — the whole point of pinning is that the device must run
 * the authorized version or nothing — so every failure here is a refusal the run
 * reports, never a fallback to a same-named directory.
 *
 * Pure over an injected `fetch`, so the whole exchange is unit-tested without a
 * server, exactly as `root-registration.ts` is.
 */

import type { DeclaredSkill, SkillCache } from './skill-cache'
import type { PackageEntry } from './skill-package'

/** What one pull needs from the command the device was handed. */
export interface SkillPackageRequest {
  origin: string
  nativeBearer: string
  /** The command this run belongs to; the server checks the version against it. */
  commandId: string
  deviceId: string
  bindingId: string
  workspaceId: string
  grantVersion: number
  paramsDigest: string
  skillId: string
  digest: string
  fetchFn?: typeof fetch
}

export type SkillPackageResult =
  | { ok: true; entries: PackageEntry[] }
  | { ok: false; code: string; message: string }

/** The one path a device pulls a skill package from (contracts/desktop/v2.json). */
export const SKILL_PACKAGE_PATH = '/api/desktop/execution/skill-package'

function decodeEntries(payload: unknown): PackageEntry[] | string {
  const record = payload as Record<string, unknown> | null
  const raw = record?.entries
  if (!Array.isArray(raw)) return 'the skill package response has no entries'
  const entries: PackageEntry[] = []
  for (const item of raw) {
    const entry = item as Record<string, unknown> | null
    const relativePath = typeof entry?.relative_path === 'string' ? entry.relative_path : ''
    const encoded = typeof entry?.body_base64 === 'string' ? entry.body_base64 : ''
    if (!relativePath) return 'a skill package entry has no relative_path'
    // Strict base64: `Buffer.from(..., 'base64')` ignores stray characters, so a
    // body that is not base64 would decode to *something* and then fail the
    // digest check with a message about the package rather than about the
    // transport. Round-tripping the length makes the failure honest.
    const body = Buffer.from(encoded, 'base64')
    if (body.toString('base64').replace(/=+$/, '') !== encoded.replace(/=+$/, '')) {
      return `skill package entry ${relativePath} is not base64`
    }
    entries.push({ relativePath, body })
  }
  return entries
}

/**
 * Pull one skill version.
 *
 * Every field is validated before a request is made, because a request this
 * process cannot describe is one the server cannot authorize -- and a half-named
 * request answered with a 400 would read as "the server refused this skill"
 * rather than "this device asked wrong".
 */
export async function fetchSkillPackage(
  request: SkillPackageRequest,
): Promise<SkillPackageResult> {
  for (const [name, value] of [
    ['origin', request.origin], ['nativeBearer', request.nativeBearer],
    ['commandId', request.commandId], ['deviceId', request.deviceId],
    ['bindingId', request.bindingId], ['workspaceId', request.workspaceId],
    ['paramsDigest', request.paramsDigest], ['skillId', request.skillId],
    ['digest', request.digest],
  ] as const) {
    if (!value) return { ok: false, code: 'invalid_request', message: `${name} is required` }
  }
  if (!Number.isInteger(request.grantVersion) || request.grantVersion <= 0) {
    return { ok: false, code: 'invalid_request', message: 'grantVersion must be a positive integer' }
  }
  const query = new URLSearchParams({
    command_id: request.commandId,
    device_id: request.deviceId,
    binding_id: request.bindingId,
    workspace_id: request.workspaceId,
    grant_version: String(request.grantVersion),
    params_digest: request.paramsDigest,
    skill_id: request.skillId,
    digest: request.digest,
  })
  const url = `${request.origin.replace(/\/+$/, '')}${SKILL_PACKAGE_PATH}?${query.toString()}`
  const fetchFn = request.fetchFn ?? fetch
  let response: Response
  try {
    response = await fetchFn(url, {
      method: 'GET',
      headers: { Authorization: `Bearer ${request.nativeBearer}` },
    })
  } catch (err) {
    return { ok: false, code: 'backend_unavailable', message: String((err as Error)?.message || err) }
  }
  const text = await response.text().catch(() => '')
  let payload: Record<string, unknown> = {}
  try {
    payload = text ? (JSON.parse(text) as Record<string, unknown>) : {}
  } catch {
    payload = {}
  }
  if (!response.ok) {
    const code = typeof payload.code === 'string' && payload.code ? payload.code : 'skill_unavailable'
    const message = typeof payload.message === 'string' && payload.message
      ? payload.message
      : `the server refused the skill package (HTTP ${response.status})`
    return { ok: false, code, message }
  }
  // The server's own answer must be the version that was asked for. A response
  // for a different digest would otherwise be installed as the version the run
  // needs, which is the substitution this whole path exists to prevent.
  if (String(payload.digest || '') !== request.digest) {
    return {
      ok: false, code: 'incompatible_skill',
      message: `the server answered with ${String(payload.digest || 'no digest')} `
        + `for a request of ${request.digest}`,
    }
  }
  const decoded = decodeEntries(payload)
  if (typeof decoded === 'string') {
    return { ok: false, code: 'incompatible_skill', message: decoded }
  }
  return { ok: true, entries: decoded }
}

/** Everything one run's skill delivery needs, injected so it is testable. */
export interface SkillDeliveryRequest {
  cache: SkillCache | null
  declared: readonly DeclaredSkill[]
  origin: string
  nativeBearer: string
  commandId: string
  deviceId: string
  bindingId: string
  workspaceId: string
  grantVersion: number
  paramsDigest: string
  fetchFn?: typeof fetch
}

/**
 * Make sure every declared version is in the cache, or refuse.
 *
 * Only the *missing* versions are fetched: a device that already holds the
 * version has nothing to ask for, and re-downloading it on every call would turn
 * a cheap check into a per-call transfer. What was already here is still
 * verified -- by `resolveDeclared` on the device-execution side, which is the
 * gate that decides whether the run may proceed.
 *
 * The first failure stops the delivery. Partial success would be worse than
 * none: the run must have *all* the versions it was authorized with, and
 * installing some of them would leave a device that looks partly ready for a run
 * that must not start.
 */
export async function ensureSkillVersions(
  request: SkillDeliveryRequest,
): Promise<{ ok: true } | { ok: false; code: string; message: string }> {
  const cache = request.cache
  if (!cache) {
    return {
      ok: false, code: 'skill_cache_unavailable',
      message: '本机没有技能缓存，无法接收技能包。',
    }
  }
  for (const declared of request.declared) {
    if (cache.has(declared.skillId, declared.digest)) continue
    const fetched = await fetchSkillPackage({
      origin: request.origin,
      nativeBearer: request.nativeBearer,
      commandId: request.commandId,
      deviceId: request.deviceId,
      bindingId: request.bindingId,
      workspaceId: request.workspaceId,
      grantVersion: request.grantVersion,
      paramsDigest: request.paramsDigest,
      skillId: declared.skillId,
      digest: declared.digest,
      ...(request.fetchFn ? { fetchFn: request.fetchFn } : {}),
    })
    if (!fetched.ok) return fetched
    try {
      // The install is where the bytes are actually checked: the declared digest
      // is recomputed over the resource set, so a truncated or substituted
      // package is refused here and leaves no directory behind.
      cache.install(declared.skillId, declared.digest, fetched.entries)
    } catch (err) {
      const code = (err as { code?: unknown })?.code
      return {
        ok: false,
        code: typeof code === 'string' && code ? code : 'incompatible_skill',
        message: (err as Error)?.message || String(err),
      }
    }
  }
  return { ok: true }
}
