/**
 * What may enter a skill package, and the digest over one (task 8.9).
 *
 * The server builds a skill package and the device verifies it. If those two
 * ends each carried their own copy of "is this path safe" / "is this a
 * credential" / "what is this package's digest", they would drift, and the drift
 * would be invisible in the worst way: the device rejecting a valid package looks
 * like a transfer bug, and the device *accepting* an invalid one looks like
 * nothing at all until it is exploited.
 *
 * So this file is a deliberate, byte-for-byte port of
 * `agent/desktop_local/package_rules.py` (`digest_of`, `is_secret_name`,
 * `refuse_secrets`, `safe_relative`) and of the two digest functions in
 * `agent/skills/manifest.py` (`_walk_resources`' per-file digest and
 * `_content_digest`). The Python side is the definition; this side must agree
 * with it exactly, and `tests/test_desktop_skill_package_digest.cjs` proves it by
 * having the real Python compute the same value rather than by re-deriving an
 * expectation here.
 *
 * Why the algorithm is not simply "hash the tarball": the declared digest is
 * computed over the *resource set*, so it must change if a file is added,
 * removed, renamed or edited -- the four ways a package differs while still
 * looking like "the same skill". A container format would also make the digest
 * depend on compression and on entry order, neither of which is a property of
 * the skill.
 *
 * Nothing here touches the filesystem: these are pure functions over strings and
 * buffers, which is what lets the verifier be tested without a cache and the
 * cache be built without a transfer.
 */

import * as crypto from 'node:crypto'

/** A refused package entry, with a stable machine-readable `code`. */
export class SkillPackageError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(`${code}: ${message}`)
    this.name = 'SkillPackageError'
    this.code = code
  }
}

/**
 * Basenames that are credentials by convention. A skill never legitimately ships
 * these, and "the package happened to include one" is how a skill becomes a
 * credential-exfiltration route.
 */
export const SECRET_BASENAMES: ReadonlySet<string> = new Set([
  '.env', '.netrc', '.pgpass', 'credentials', 'credentials.json',
  'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', 'identity.db',
])

/** Extensions that carry keys or stores rather than skill content. */
export const SECRET_SUFFIXES: readonly string[] = [
  '.key', '.pem', '.p12', '.pfx', '.keystore', '.jks',
]

/**
 * Path segments that must never appear: a VCS directory holds history and remote
 * credentials, not skill resources.
 */
export const SECRET_SEGMENTS: ReadonlySet<string> = new Set([
  '.git', '.ssh', '.aws', '.gnupg',
])

/** One entry of a skill package: a relative path and the bytes at it. */
export interface PackageEntry {
  relativePath: string
  body: Buffer
}

/** A verified resource: what the digest is computed over. */
export interface PackageResource {
  relativePath: string
  digest: string
  size: number
}

/** `sha256:<hex>` over a payload. One spelling, both ends. */
export function digestOf(payload: Buffer): string {
  return `sha256:${crypto.createHash('sha256').update(payload).digest('hex')}`
}

/** Whether a package-relative path names a credential. */
export function isSecretName(relative: string): boolean {
  const parts = relative.replace(/\\/g, '/').split('/')
  const basename = parts[parts.length - 1].toLowerCase()
  if (parts.slice(0, -1).some((segment) => SECRET_SEGMENTS.has(segment))) {
    return true
  }
  if (SECRET_BASENAMES.has(basename) || basename.startsWith('.env')) {
    return true
  }
  return SECRET_SUFFIXES.some((suffix) => basename.endsWith(suffix))
}

/** `isSecretName`, as a refusal. */
export function refuseSecrets(relative: string): void {
  if (isSecretName(relative)) {
    throw new SkillPackageError(
      'secret_refused', `${JSON.stringify(relative)} is a credential, not skill content`)
  }
}

/**
 * Validate a package-relative path, or refuse it.
 *
 * The refusals matter more than the acceptance: this is the only thing between a
 * manifest written by a remote server and a write anywhere on the machine.
 * Returning the *normalised* form (forward slashes, no `.` segments) rather than
 * the input keeps the two ends agreeing on how a path is spelled, which is what
 * lets a manifest entry and a payload key be compared at all.
 */
export function safeRelative(raw: unknown): string {
  if (typeof raw !== 'string' || !raw.trim()) {
    throw new SkillPackageError('path_outside_package', 'empty resource path')
  }
  const text = raw.trim().replace(/\\/g, '/')
  if (text.startsWith('/') || /^[A-Za-z]:/.test(text)) {
    throw new SkillPackageError(
      'path_outside_package', `${JSON.stringify(raw)} is absolute; resources are relative`)
  }
  const parts = text.split('/')
  for (const part of parts) {
    if (part === '' || part === '.' || part === '..') {
      throw new SkillPackageError(
        'path_outside_package',
        `${JSON.stringify(raw)} is not a normalised relative path`)
    }
  }
  return parts.join('/')
}

/**
 * Normalise a package to its resource list, in the canonical order.
 *
 * Applies the same three refusals the Python walker applies (`safe_relative`,
 * `refuse_secrets`, and a duplicate-path check that mirrors "one entry per file
 * on disk"), so a package this accepts is one the server would have produced.
 * The sort is *inside* the function rather than left to the caller: a digest that
 * is only stable because the caller happened to sort first is one refactor away
 * from making every package look like a new version.
 */
export function packageResources(entries: readonly PackageEntry[]): PackageResource[] {
  const seen = new Set<string>()
  const resources: PackageResource[] = []
  for (const entry of entries) {
    const relativePath = safeRelative(entry.relativePath)
    refuseSecrets(relativePath)
    if (seen.has(relativePath)) {
      throw new SkillPackageError(
        'duplicate_resource', `${JSON.stringify(relativePath)} appears twice in one package`)
    }
    seen.add(relativePath)
    resources.push({
      relativePath,
      digest: digestOf(entry.body),
      size: entry.body.length,
    })
  }
  return resources.sort((a, b) => (a.relativePath < b.relativePath ? -1
    : a.relativePath > b.relativePath ? 1 : 0))
}

/**
 * One digest over the whole resource set, independent of entry order.
 *
 * Each entry contributes its path, its content digest and its size, so the result
 * changes if a file is added, removed, renamed, or edited -- the four ways a
 * package can differ while still looking like "the same skill".
 *
 * The separators are the Python ones (`\0` between fields, `\1` between entries)
 * and the encoding is UTF-8: a digest is only comparable across the two ends if
 * the bytes hashed are identical, not merely "equivalent".
 */
export function contentDigest(resources: readonly PackageResource[]): string {
  const sorted = [...resources].sort((a, b) => (a.relativePath < b.relativePath ? -1
    : a.relativePath > b.relativePath ? 1 : 0))
  const material = sorted
    .map((resource) => `${resource.relativePath}\u0000${resource.digest}\u0000${resource.size}\u0001`)
    .join('')
  return `sha256:${crypto.createHash('sha256').update(material, 'utf8').digest('hex')}`
}

/**
 * The digest a package has, computed from its bytes, or a refusal.
 *
 * This is the one call the verifier makes: everything above is exposed for tests
 * and for the error codes, but "what digest is this package" has to be a single
 * answer or the two ends can disagree about which rule applied.
 */
export function packageDigest(entries: readonly PackageEntry[]): string {
  return contentDigest(packageResources(entries))
}
