// Save-as on the client: the phase-1 download policy (tasks 5.6/5.7).
//
// W19 lets a user keep a file the server produced. The page must not be able to
// turn that into a file write anywhere: it may name a *server* URL, and the
// host decides everything else -- the destination directory, whether an
// existing file is replaced, and how many bytes are allowed through. Three
// rules carry that boundary, and all three are pure data checks, so they live
// here (no Electron import) and ``tests/test_desktop_downloads.cjs`` drives
// them for real:
//
//   * the target is the authorized origin over http(s), on a declared download
//     or document prefix, with no credential in the query -- the guest cookie
//     is the only authentication a download link may carry, so a ``?token=`` in
//     a link (or in an ``artifact_ref`` the server sent) is refused rather than
//     fetched;
//   * an ``artifact_ref`` is a server-relative path; anything already shaped
//     like a URL (absolute, protocol-relative, or with a scheme) is refused
//     instead of being normalized into a target;
//   * the received bytes are counted against ``limits.file_max_bytes`` and the
//     write goes to a temporary sibling first, so a foreign redirect, an
//     over-budget stream, or a change of session never leaves a half-written
//     file with the real name.
//
// ``installDownloadPolicy`` is the Electron half: it is deliberately thin (it
// wires ``will-download`` to the decisions above) and takes its file-system
// operations by injection so a test can watch every write.

import type { Session } from 'electron'

/**
 * Paths the host may take a *document* from (rendered in the isolated content
 * window) and paths it may take an *attachment* from. Mirrored from
 * ``contracts/desktop/v1.json`` §downloads; the contract test asserts the two
 * spellings agree.
 */
export const DOWNLOAD_DOCUMENT_PREFIXES = ['/preview', '/uploads']

export const DOWNLOAD_ATTACHMENT_PREFIXES = [
  '/api/file', '/api/workspace', '/api/knowledge', '/api/logs', '/api/artifacts',
]

/**
 * Query parameter names that must never appear in a download URL.
 *
 * A paired guest session authenticates with its cookie. Accepting a credential
 * in a link would create a second way in -- one that keeps working after the
 * pairing is revoked -- so the check is a refusal, not a strip.
 */
export const FORBIDDEN_QUERY_KEYS = [
  'token', 'access_token', 'accesstoken', 'auth', 'authorization', 'signature',
  'sig', 'secret', 'password', 'passwd', 'key', 'apikey', 'api_key',
  'credential', 'code', 'bootstrap',
]

/** Bytes per read; the guest never holds the whole stream in memory. */
export const DOWNLOAD_CHUNK_BYTES = 1024 * 1024

/** Suffix of the temporary sibling a write goes to before its real name. */
export const DOWNLOAD_TEMP_SUFFIX = '.cowpart'

/** Longest local file name the host will write. */
export const DOWNLOAD_FILENAME_MAX_CHARS = 180

/** ``limits.file_max_bytes``: also the ceiling for one download. */export const DOWNLOAD_MAX_BYTES = 536870912

export interface DownloadContext {
  /** The exact backend origin this container was authorized against. */
  origin: string
  /** Shell entry paths the server advertised (kept for a content decision). */
  entryPaths: string[]
}

export type DownloadTarget =
  | { ok: true; url: string; kind: 'document' | 'attachment'; path: string }
  | { ok: false; code: string; message: string }

function refuse(code: string, message: string): DownloadTarget {
  return { ok: false, code, message }
}

const inPrefixes = (path: string, prefixes: string[]): boolean =>
  prefixes.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))

function parseUrl(raw: string): URL | null {
  try {
    const parsed = new URL(String(raw || ''))
    // A URL always has a protocol when it parses; ``about:blank`` and a bare
    // path do not reach here as usable targets.
    return parsed.protocol ? parsed : null
  } catch {
    return null
  }
}

/** Same origin means scheme, host and port all match (no userinfo allowed). */
function sameOrigin(a: URL, origin: URL): boolean {
  if (a.username || a.password) return false
  return a.origin === origin.origin
}

/**
 * Decide whether a guest-supplied URL may be fetched for saving.
 *
 * Returns the target on success. Every refusal names why, because the UI has to
 * tell the user "this link carries a token" rather than "download failed".
 */
export function classifyDownloadTarget(raw: string, context: DownloadContext): DownloadTarget {
  const parsed = parseUrl(raw)
  if (!parsed) return refuse('invalid_request', 'the download target is not a URL')
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return refuse('unsafe_path', 'the download target is not a web URL')
  }
  const origin = parseUrl(context.origin)
  if (!origin) return refuse('invalid_request', 'the authorized origin is not a URL')
  if (parsed.protocol === 'http:' && origin.protocol === 'https:') {
    // A plain-HTTP target would leave the paired session behind; never a
    // downgrade, even when the host names resolve to the same place.
    return refuse('unsafe_path', 'the download target would downgrade to plain HTTP')
  }
  if (!sameOrigin(parsed, origin)) {
    return refuse('unsafe_path', 'the download target is not on the authorized origin')
  }
  for (const [key] of parsed.searchParams) {
    if (FORBIDDEN_QUERY_KEYS.includes(key.toLowerCase())) {
      return refuse('unsafe_path', `the download target carries a credential in '${key}'`)
    }
  }
  const document = inPrefixes(parsed.pathname, DOWNLOAD_DOCUMENT_PREFIXES)
  const attachment = inPrefixes(parsed.pathname, DOWNLOAD_ATTACHMENT_PREFIXES)
  if (!document && !attachment) {
    return refuse('unsafe_path', 'the download target is not a declared download or document path')
  }
  return { ok: true, url: parsed.toString(), kind: document ? 'document' : 'attachment', path: parsed.pathname }
}

/**
 * Resolve a server ``artifact_ref`` to a fetchable same-origin URL.
 *
 * An artifact reference is a *relative* path. A value that is already a URL is
 * refused rather than normalized -- that is how a server (or a compromised page)
 * could otherwise point the host at another host, or smuggle a token past
 * ``classifyDownloadTarget``.
 */
export function resolveArtifactRef(ref: string, context: DownloadContext): DownloadTarget {
  const value = String(ref || '')
  if (!value) return refuse('invalid_request', 'the artifact_ref is empty')
  if (value.startsWith('//')) {
    return refuse('unsafe_path', 'an artifact_ref must not be a protocol-relative URL')
  }
  const withoutQuery = value.split('?', 1)[0].split('#', 1)[0]
  if (!withoutQuery.startsWith('/')) {
    return refuse('unsafe_path', 'an artifact_ref must be a server-relative path')
  }
  if (withoutQuery.split('/').slice(1).some((part) => part === '' || part === '.' || part === '..')) {
    return refuse('unsafe_path', 'an artifact_ref must not contain an empty, dot or dot-dot component')
  }
  return classifyDownloadTarget(`${context.origin.replace(/\/+$/, '')}${value}`, context)
}

/**
 * A local file name derived only from the server's suggestion.
 *
 * The guest contributes a name, never a path: separators, control characters,
 * trailing dots/spaces, Windows reserved devices and a leading dot are all
 * removed, and the result is bounded. Chinese names survive (they are ordinary
 * characters, not a hazard), and if nothing usable is left the caller gets a
 * fixed fallback rather than an empty string.
 */
export function sanitizeFileName(raw: string, fallback = 'download'): string {
  let name = String(raw || '')
  // Both separators, because the guest runs on the platform that will write it.
  const lastSeparator = Math.max(name.lastIndexOf('/'), name.lastIndexOf('\\'))
  if (lastSeparator >= 0) name = name.slice(lastSeparator + 1)
  // Control characters, plus the ones a shell or a directory listing would eat.
  name = name.replace(/[\u0000-\u001f\u007f]/g, '')
  name = name.replace(/[:*?"<>|]/g, '_')
  name = name.replace(/\.+$/, '')
  name = name.replace(/^\.+/, '')
  name = name.trim().replace(/\s+/g, ' ')
  if (!name) return fallback
  const stem = name.split('.')[0].toUpperCase()
  const reserved = /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/
  if (reserved.test(stem)) name = `_${name}`
  if (name.length > DOWNLOAD_FILENAME_MAX_CHARS) {
    const dot = name.lastIndexOf('.')
    const extension = dot > 0 ? name.slice(dot) : ''
    const keep = Math.max(1, DOWNLOAD_FILENAME_MAX_CHARS - extension.length)
    name = name.slice(0, keep) + extension
  }
  return name || fallback
}

/** The temporary sibling a write goes to, so the real name appears atomically. */
export function temporaryPath(target: string, nonce: string): string {
  return `${target}${DOWNLOAD_TEMP_SUFFIX}.${nonce}`
}

export interface DownloadDestination {
  /** Absolute path the host will write. */
  path: string
  /** True when the path already exists and must be confirmed before writing. */
  exists: boolean
}

/**
 * Plan the destination for one download.
 *
 * The page never supplies the directory; ``directory`` is the host's choice.
 * An existing file is *reported*, never overwritten: the caller asks the user
 * and re-issues with an explicit confirmation, which is what makes "取消" and
 * "显式覆盖确认" the same code path rather than two behaviours to keep in sync.
 */
export function planDestination(
  directory: string,
  fileName: string,
  exists: (path: string) => boolean,
): DownloadDestination {
  const separator = directory.endsWith('/') || directory.endsWith('\\') ? '' : '/'
  const path = `${directory}${separator}${sanitizeFileName(fileName)}`
  return { path, exists: exists(path) }
}

export type DownloadOutcome =
  | { ok: true; path: string; bytes: number }
  | { ok: false; code: string; message: string; bytes: number }

/**
 * A byte budget for one download.
 *
 * ``seen`` grows as the stream arrives; ``accept`` answers whether another
 * chunk may be taken. The ceiling is the contract's ``file_max_bytes``, and an
 * unknown total is treated as "accept until the ceiling is reached" so a
 * chunked response cannot stream past it.
 */
export class DownloadBudget {
  private seen = 0

  constructor(
    private readonly maxBytes: number = DOWNLOAD_MAX_BYTES,
    private readonly chunkBytes: number = DOWNLOAD_CHUNK_BYTES,
  ) {}

  /** Bytes counted so far. */
  get received(): number {
    return this.seen
  }

  /** Chunk size the stream should use. */
  get chunk(): number {
    return this.chunkBytes
  }

  /** Count ``bytes`` and answer whether the stream may continue. */
  accept(bytes: number): boolean {
    if (typeof bytes !== 'number' || Number.isNaN(bytes) || bytes < 0) return false
    const next = this.seen + bytes
    if (next > this.maxBytes) return false
    this.seen = next
    return true
  }

  /** The refusal for a stream that ran past the ceiling. */
  exceeded(): DownloadOutcome {
    return {
      ok: false,
      code: 'limit_exceeded',
      message: `the download exceeds ${this.maxBytes} bytes`,
      bytes: this.seen,
    }
  }

  /** The outcome of a completed stream, given the server's own total (if any). */
  complete(total: number | null): DownloadOutcome {
    if (typeof total === 'number' && total >= 0 && total !== this.seen) {
      return {
        ok: false,
        code: 'checksum_mismatch',
        message: `the download ended at ${this.seen} of ${total} bytes`,
        bytes: this.seen,
      }
    }
    return { ok: true, path: '', bytes: this.seen }
  }
}

/** The minimum surface ``installDownloadPolicy`` needs from an Electron item. */
export interface DownloadItemLike {
  getURL(): string
  getFilename(): string
  getTotalBytes(): number
  getReceivedBytes(): number
  setSavePath(path: string): void
  cancel(): void
  on(event: string, listener: (...args: unknown[]) => void): void
}

export interface DownloadSessionLike {
  on(event: 'will-download', listener: (event: unknown, item: DownloadItemLike) => void): void
  off?(event: 'will-download', listener: (event: unknown, item: DownloadItemLike) => void): void
}

export interface DownloadPolicyOptions {
  session: DownloadSessionLike
  origin: string
  entryPaths: string[]
  /** The host's chosen directory. The page never supplies this. */
  directory: string
  /** Names the user explicitly confirmed for replacement (this session). */
  confirmedOverwrites?: Set<string>
  exists?: (path: string) => boolean
  rename?: (from: string, to: string) => void
  remove?: (path: string) => void
  nonce?: () => string
  /** Observer for the UI; receives no URL query and no file content. */
  onEvent?: (event: DownloadEvent) => void
}

export interface DownloadEvent {
  phase: 'started' | 'refused' | 'needs_confirmation' | 'completed' | 'cancelled' | 'failed'
  code: string
  /** Sanitized file name, never a path a page could have chosen. */
  fileName: string
  bytes: number
}

/**
 * Install the phase-1 download policy on a container session.
 *
 * Returns a disposer. The decisions themselves are the pure functions above;
 * this only connects them to Electron's download lifecycle, and every write is
 * taken as an injected operation so a test can assert the *sequence* -- nothing
 * is written under the real name until the bounded stream finished and the
 * bytes matched what the server promised.
 */
export function installDownloadPolicy(options: DownloadPolicyOptions): () => void {
  const exists = options.exists ?? (() => false)
  const rename = options.rename ?? (() => undefined)
  const remove = options.remove ?? (() => undefined)
  const nonce = options.nonce ?? (() => `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`)
  const confirmed = options.confirmedOverwrites ?? new Set<string>()
  const emit = options.onEvent ?? (() => undefined)

  const handler = (_event: unknown, item: DownloadItemLike) => {
    const context: DownloadContext = { origin: options.origin, entryPaths: options.entryPaths }
    const target = classifyDownloadTarget(item.getURL(), context)
    const suggested = sanitizeFileName(item.getFilename())
    if (!target.ok) {
      // A download the container did not sanction never touches the disk.
      item.cancel()
      emit({ phase: 'refused', code: target.code, fileName: suggested, bytes: 0 })
      return
    }
    const plan = planDestination(options.directory, item.getFilename(), exists)
    if (plan.exists && !confirmed.has(plan.path)) {
      // No silent replacement: the user decides, and the download is cancelled
      // until they do. Re-issuing with the name in ``confirmedOverwrites`` is
      // the only way this path is written.
      item.cancel()
      emit({ phase: 'needs_confirmation', code: 'file_changed', fileName: plan.path, bytes: 0 })
      return
    }

    const partial = temporaryPath(plan.path, nonce())
    item.setSavePath(partial)
    const budget = new DownloadBudget()
    emit({ phase: 'started', code: 'ok', fileName: plan.path, bytes: 0 })

    let finished = false
    item.on('updated', () => {
      const received = Number(item.getReceivedBytes()) || 0
      const total = Number(item.getTotalBytes()) || 0
      const room = received - budget.received
      if (room > 0 && !budget.accept(room)) {
        item.cancel()
        finished = true
        remove(partial)
        emit({ phase: 'failed', code: 'limit_exceeded', fileName: plan.path, bytes: budget.received })
        return
      }
      if (total > DOWNLOAD_MAX_BYTES) {
        item.cancel()
        finished = true
        remove(partial)
        emit({ phase: 'failed', code: 'limit_exceeded', fileName: plan.path, bytes: budget.received })
      }
    })
    item.on('done', (...args: unknown[]) => {
      if (finished) return
      finished = true
      const state = String(args[1] ?? '')
      if (state !== 'completed') {
        remove(partial)
        emit({
          phase: state === 'cancelled' ? 'cancelled' : 'failed',
          code: state === 'cancelled' ? 'cancelled' : 'interrupted',
          fileName: plan.path,
          bytes: budget.received,
        })
        return
      }
      const outcome = budget.complete(Number(item.getTotalBytes()) || null)
      if (!outcome.ok) {
        remove(partial)
        emit({ phase: 'failed', code: outcome.code, fileName: plan.path, bytes: outcome.bytes })
        return
      }
      // The bytes matched: the temporary file becomes the real one.
      rename(partial, plan.path)
      confirmed.add(plan.path)
      emit({ phase: 'completed', code: 'ok', fileName: plan.path, bytes: outcome.bytes })
    })
  }

  options.session.on('will-download', handler)
  return () => {
    if (typeof options.session.off === 'function') options.session.off('will-download', handler)
  }
}

/** Kept for the container wiring: a typed narrowing of the Electron session. */
export const asDownloadSession = (session: Session): DownloadSessionLike =>
  session as unknown as DownloadSessionLike
