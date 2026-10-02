/**
 * Isolated preview of a local project file (task 9.5, acceptance A25).
 *
 * A local file has no URL. There is no `/preview/<token>` route that could serve
 * it -- inventing one would serve the *server's* file of the same name -- and
 * handing the page a `file://` path would be worse than useless: it publishes a
 * permanent, guessable address for the user's disk, and the shell document is the
 * one document that *does* hold the bridge.
 *
 * So the bytes take the long way round and never reach the page:
 *
 *   1. the caller re-verifies the file exactly as the system actions do
 *      (`planNativeAction`: live grant, real path inside the project, links
 *      refused, kind unchanged on disk);
 *   2. the bytes are read *here*, bounded;
 *   3. they are parked in a ticket -- short-lived, one file, no path -- and the
 *      page is handed `cow-preview://preview/<ticket>/<name>`;
 *   4. the protocol handler answers only while that ticket lives, and stamps
 *      every answer with the restrictive CSP for that kind (``sandbox``, so the
 *      document runs in an **opaque origin**, and ``default-src 'none'``, so it
 *      fetches nothing).
 *
 * What the page gets is a URL that stops working: no path, no token it can mint
 * again, and no bytes it can read back into script (the scheme is not
 * fetch-enabled). What it *embeds* is a frame or a media element that renders
 * under a policy the page cannot relax -- see `execution-isolation` and task
 * 9.5's evidence for why the surface is a sandboxed subframe rather than a
 * window, and for the probe that measured it.
 *
 * Pure Node (no Electron import): the ticket store takes its clock and its
 * randomness by injection, so a test can expire a ticket without waiting and can
 * assert that a token is not derivable from the file's name.
 */

import { planNativeAction, type NativeActionRefusal } from './native-actions'

/** The scheme the protected preview resources live at. */
export const PREVIEW_SCHEME = 'cow-preview'

/**
 * The one host this scheme serves, so the ticket never has to be a hostname.
 *
 * A per-ticket host was the first shape and it is a trap: an opaque host is
 * compared byte-for-byte by Node and *case-folded* by Chromium, so a token that
 * ever contained a capital letter would resolve one way in the handler and
 * another in the window. A fixed host removes the question. The token is in the
 * path, where both parsers agree.
 */
export const PREVIEW_HOST = 'preview'

/**
 * The privileges the scheme is registered with, as a value rather than a literal.
 *
 * Declared here because *every* surface that registers the scheme has to agree on
 * them -- the application registers them once at startup, and the isolation probe
 * (`desktop/e2e/preview-isolation.probe.cjs`) registers the same set to measure
 * what a page can and cannot do with a preview URL. Two lists would drift, and
 * the drift would show up as "the probe says images are blocked" or worse: as a
 * privilege granted in the app that nobody measured.
 *
 * `supportFetchAPI` and `corsEnabled` are deliberately absent. With them the
 * container document could `fetch()` a preview URL and read a local file's bytes
 * into script -- a capability the page has no use for, since what it does with a
 * preview is *display* it. Without them the URL is embeddable and not readable.
 * `bypassCSP` stays off, which is what makes the response's policy binding.
 */
export const PREVIEW_SCHEME_PRIVILEGES = {
  standard: true,
  secure: true,
  stream: true,
}

/**
 * The largest file the isolated preview will carry.
 *
 * Not a contract value: a preview is "look at this", and an HTML report that is
 * larger than this is one the user should open in a real application. The bound
 * exists so a 2 GiB file cannot be pulled into the main process by a click.
 */
export const PREVIEW_MAX_BYTES = 8 * 1024 * 1024

/**
 * How long a ticket lives.
 *
 * Long enough for the window to load the document and for the document to load
 * what it needs from *the same ticket* (a reload, a ranged read of the same
 * file), short enough that a URL which leaked would be worthless. There is no
 * way to extend it: a new preview mints a new ticket.
 */
export const PREVIEW_TICKET_TTL_MS = 120_000

/** How many tickets may be alive at once. The oldest is evicted beyond this. */
export const PREVIEW_TICKET_MAX = 8

/** The ways one local file may be rendered by the isolated surface. */
export type PreviewKind = 'html' | 'image' | 'media'

/**
 * One table for both the kind and the content type.
 *
 * Two tables would be two chances to disagree, and a `.svg` classified as an
 * image while being served as `image/svg+xml` in a *scriptless* policy would
 * quietly break the only kind of preview that legitimately needs scripts.
 *
 * PDF is **not** here, and that is a measurement rather than an omission: this
 * shell runs with `plugins: false`, so Chromium has no PDF viewer and an
 * `application/pdf` frame renders blank. Saying "no preview for this kind, open
 * it with a system application" is the honest answer, and 9.4 already puts that
 * button in the panel. See `evidence/isolated-preview.md` §2.
 */
const PREVIEW_TYPES: Record<string, { kind: PreviewKind; contentType: string }> = {
  html: { kind: 'html', contentType: 'text/html; charset=utf-8' },
  htm: { kind: 'html', contentType: 'text/html; charset=utf-8' },
  svg: { kind: 'html', contentType: 'image/svg+xml' },
  png: { kind: 'image', contentType: 'image/png' },
  jpg: { kind: 'image', contentType: 'image/jpeg' },
  jpeg: { kind: 'image', contentType: 'image/jpeg' },
  gif: { kind: 'image', contentType: 'image/gif' },
  webp: { kind: 'image', contentType: 'image/webp' },
  bmp: { kind: 'image', contentType: 'image/bmp' },
  ico: { kind: 'image', contentType: 'image/x-icon' },
  avif: { kind: 'image', contentType: 'image/avif' },
  mp4: { kind: 'media', contentType: 'video/mp4' },
  webm: { kind: 'media', contentType: 'video/webm' },
  mov: { kind: 'media', contentType: 'video/quicktime' },
  mp3: { kind: 'media', contentType: 'audio/mpeg' },
  wav: { kind: 'media', contentType: 'audio/wav' },
  m4a: { kind: 'media', contentType: 'audio/mp4' },
  ogg: { kind: 'media', contentType: 'audio/ogg' },
  flac: { kind: 'media', contentType: 'audio/flac' },
}

/** The kind of preview one file is entitled to, or `''` for "not this surface". */
export function previewKindOf(name: string): PreviewKind | '' {
  const entry = PREVIEW_TYPES[extensionOf(name)]
  return entry ? entry.kind : ''
}

/** The content type one previewed file is served with. */
export function previewContentType(name: string): string {
  const entry = PREVIEW_TYPES[extensionOf(name)]
  return entry ? entry.contentType : 'application/octet-stream'
}

/**
 * The policy every answer from the preview scheme carries.
 *
 * `default-src 'none'` first, so anything not named here is refused -- the
 * document cannot fetch, cannot post a form, cannot frame, cannot pull a plugin,
 * cannot even pick its own `<base>`. What is named is what a *report* needs and
 * nothing more:
 *
 *   * `img-src`/`media-src` `data: blob:` and `style-src`/`script-src`
 *     `'unsafe-inline'` -- a self-contained generated page carries its own
 *     markup, its own stylesheet and its own script, as inline text. (External
 *     assets are *not* served: the ticket is for one file, and a document that
 *     wants its siblings is not a document this surface can show. That is a
 *     boundary, written down in the evidence, not an oversight.)
 *   * `sandbox allow-scripts` for markup -- scripts run, but in an **opaque
 *     origin**: no cookie, no storage, no same-origin anything. `allow-same-origin`
 *     is deliberately absent, which is what makes the previous sentence true.
 *     `allow-popups` is absent too: a report that tries to open a window gets
 *     `null`, because a preview is not a place to launch things from.
 *   * the inert kinds get no scripts at all: an image or a recording is data.
 *
 * The ticket is what authorizes the request, and it is not a cookie, so there is
 * nothing to steal from it either.
 */
export function previewContentSecurityPolicy(kind: PreviewKind): string {
  const common = [
    "default-src 'none'",
    "connect-src 'none'",
    "form-action 'none'",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    'sandbox',
  ]
  if (kind === 'html') {
    return [
      "default-src 'none'",
      "img-src data: blob:",
      "media-src data: blob:",
      "font-src data:",
      "style-src 'unsafe-inline'",
      "script-src 'unsafe-inline'",
      "connect-src 'none'",
      "form-action 'none'",
      "frame-src 'none'",
      "object-src 'none'",
      "base-uri 'none'",
      'sandbox allow-scripts allow-forms allow-modals',
    ].join('; ')
  }
  // `sandbox` without `allow-scripts`, `allow-forms` or `allow-modals`: nothing
  // about a picture or a recording needs a script.
  return common.join('; ')
}

/** The headers every preview answer carries, regardless of kind. */
export function previewHeaders(name: string, kind: PreviewKind): Record<string, string> {
  return {
    'Content-Type': previewContentType(name),
    'Content-Security-Policy': previewContentSecurityPolicy(kind),
    // A preview is current by definition: caching it would let a stale copy
    // outlive the ticket that authorized it.
    'Cache-Control': 'no-store, max-age=0',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
  }
}

export interface LocalPreviewPlan {
  ok: true
  kind: PreviewKind
  /** Main-process only: the file the ticket will be minted for. */
  absolutePath: string
  name: string
  size: number
  modified: number
}

/**
 * Whether this machine may preview one project-relative file, and as what.
 *
 * The verification is *not* re-implemented: `planNativeAction` already asks the
 * live grant, the helper's own answer, the disk and the real path, and it refuses
 * the cases that matter (gone, link, changed, escaped, revoked). This adds the
 * three questions a preview has that an "open" does not: it must be a file, it
 * must fit, and it must be a kind this surface can render at all.
 */
export function planLocalPreview(input: {
  path: unknown
  root: string | null
  entry: { ok?: boolean; code?: unknown; message?: unknown; kind?: unknown; size?: unknown; modified?: unknown }
  maxBytes?: number
}): LocalPreviewPlan | NativeActionRefusal {
  const planned = planNativeAction({ kind: 'open', path: input.path, root: input.root, entry: input.entry })
  if (planned.ok !== true) return planned
  if (planned.isDirectory) {
    return refuse('not_a_file', 'a directory cannot be previewed as one document')
  }
  const max = typeof input.maxBytes === 'number' ? input.maxBytes : PREVIEW_MAX_BYTES
  if (planned.size > max) {
    return refuse('limit_exceeded', `this file is larger than the ${max} byte preview limit`)
  }
  const kind = previewKindOf(planned.name)
  if (!kind) {
    return refuse('unsupported_type', 'this kind of file has no isolated preview; open it with a system application')
  }
  return {
    ok: true,
    kind,
    absolutePath: planned.absolutePath,
    name: planned.name,
    size: planned.size,
    modified: planned.modified,
  }
}

/**
 * The metadata an issued ticket hands back to the caller.
 *
 * `url` is for the *page to embed*, not to keep: it stops answering when the
 * ticket expires (`expiresAt`), which is sent as well so the panel can say so.
 */
export interface PreviewTicket {
  token: string
  url: string
  name: string
  kind: PreviewKind
  size: number
  expiresAt: number
}

/**
 * How the bytes of one file are read for a preview.
 *
 * Injected so the bound itself -- "read at most `limit` bytes, and refuse rather
 * than truncate if there are more" -- is testable without a disk, and so the one
 * real implementation (a positional read of `limit + 1` bytes) is the only place
 * that touches the filesystem.
 */
export interface PreviewReader {
  (absolutePath: string, limit: number): Promise<Buffer>
}

/**
 * Read at most `limit` bytes from the start of a file.
 *
 * A positional read, not `readFile`: a file that grew to 2 GiB between the check
 * and the read must not be pulled into the main process before anyone notices.
 * The extra byte is the point -- a result longer than `limit` is how the caller
 * learns the file did not fit.
 */
export const defaultPreviewReader: PreviewReader = async (absolutePath, limit) => {
  // Required lazily, so this module stays importable where `node:fs` is not (a
  // page-side bundler reads the type) and so the Electron-free tests still load.
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const fs = require('fs') as typeof import('fs')
  const handle = await fs.promises.open(absolutePath, 'r')
  try {
    const buffer = Buffer.allocUnsafe(limit + 1)
    const { bytesRead } = await handle.read(buffer, 0, limit + 1, 0)
    return buffer.subarray(0, bytesRead)
  } finally {
    await handle.close()
  }
}

/**
 * The bytes of one verified preview file, bounded.
 *
 * `planLocalPreview` already refused a file whose *recorded* size did not fit;
 * this is the second half of the same decision, made against what came off the
 * disk. A byte more than the bound is a refusal, never a truncated document:
 * half an HTML report rendered as if it were whole is a lie about the file.
 */
export async function readLocalPreviewContent(input: {
  absolutePath: string
  maxBytes?: number
  read?: PreviewReader
}): Promise<{ ok: true; content: Buffer } | NativeActionRefusal> {
  const max = typeof input.maxBytes === 'number' ? input.maxBytes : PREVIEW_MAX_BYTES
  const read = input.read || defaultPreviewReader
  const content = await read(input.absolutePath, max)
  if (content.byteLength > max) {
    return refuse('limit_exceeded', `this file is larger than the ${max} byte preview limit`)
  }
  return { ok: true, content }
}

export interface PreviewTicketContent {
  content: Buffer
  name: string
  kind: PreviewKind
}

export interface PreviewTicketStore {
  /** Park one file's bytes and get the URL that may read them, until it expires. */
  issue(input: { content: Buffer | string; name: string; kind: PreviewKind }): PreviewTicket
  /** The bytes, while the ticket lives; `null` for unknown, expired or revoked. */
  contentFor(token: string): PreviewTicketContent | null
  /** Drop one ticket (the window closed, the preview was replaced). */
  revoke(token: string): void
  /** Drop all of them (the container detached, the app is quitting). */
  revokeAll(): void
  /** How many tickets are alive. For tests and diagnostics, not for decisions. */
  live(): number
}

export interface PreviewTicketOptions {
  ttlMs?: number
  max?: number
  /** Injected so a test can expire a ticket without waiting for a clock. */
  now?: () => number
  /** Injected so a test can assert the token is not derivable from the name. */
  random?: (bytes: number) => string
}

/**
 * The ticket store.
 *
 * A ticket is one file, keyed by a random token, valid for `ttlMs` and nothing
 * else. It is *not* one-shot: the window may load the document and then read it
 * again (a ranged request, a reload), and a store that answered the first read
 * and refused the second would show a blank frame. What makes it short-lived is
 * the clock and `revokeAll`, not a use counter.
 */
export function createPreviewTickets(options: PreviewTicketOptions = {}): PreviewTicketStore {
  const ttl = typeof options.ttlMs === 'number' ? options.ttlMs : PREVIEW_TICKET_TTL_MS
  const max = typeof options.max === 'number' ? options.max : PREVIEW_TICKET_MAX
  const now = options.now || (() => Date.now())
  const random = options.random || defaultRandom
  const tickets = new Map<string, { content: Buffer; name: string; kind: PreviewKind; size: number; expiresAt: number }>()

  const sweep = (at: number) => {
    for (const [token, ticket] of tickets) {
      if (ticket.expiresAt <= at) tickets.delete(token)
    }
  }

  return {
    issue(input) {
      const at = now()
      sweep(at)
      // At the bound, the *oldest* goes: a long-lived preview is the one whose
      // ticket was minted first, and it is the one least likely to be loading.
      while (tickets.size >= max) {
        const oldest = tickets.keys().next()
        if (oldest.done) break
        tickets.delete(oldest.value)
      }
      const name = String(input.name || 'preview')
      const token = random(32)
      const content = Buffer.isBuffer(input.content) ? input.content : Buffer.from(String(input.content), 'utf8')
      const expiresAt = at + ttl
      tickets.set(token, { content, name, kind: input.kind, size: content.byteLength, expiresAt })
      return {
        token,
        url: previewUrlFor(token, name),
        name,
        kind: input.kind,
        size: content.byteLength,
        expiresAt,
      }
    },
    contentFor(token) {
      const at = now()
      sweep(at)
      const ticket = tickets.get(String(token || ''))
      if (!ticket) return null
      return { content: ticket.content, name: ticket.name, kind: ticket.kind }
    },
    revoke(token) {
      tickets.delete(String(token || ''))
    },
    revokeAll() {
      tickets.clear()
    },
    live() {
      sweep(now())
      return tickets.size
    },
  }
}

/** The URL one ticket is read at: the host, the token, the file's name, no path. */
export function previewUrlFor(token: string, name: string): string {
  return `${PREVIEW_SCHEME}://${PREVIEW_HOST}/${encodeURIComponent(token)}/${encodeURIComponent(name)}`
}

/**
 * The token and name a request names, or `null` when it is not one of ours.
 *
 * Tolerant about percent-encoding on purpose: the name is *decoration* (the
 * window title, the download name) and the token is the authorization. A request
 * whose path has been re-encoded by Chromium must still find its ticket, and a
 * request that names a different file must not.
 */
function parsePreviewUrl(url: string): { token: string; name: string } | null {
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return null
  }
  if (parsed.protocol !== `${PREVIEW_SCHEME}:`) return null
  if (parsed.host !== PREVIEW_HOST) return null
  const segments = parsed.pathname.split('/').filter((part) => part !== '')
  if (segments.length < 2) return null
  const token = safeDecode(segments[0])
  const name = safeDecode(segments.slice(1).join('/'))
  if (!token || !name) return null
  return { token, name }
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return ''
  }
}

export interface PreviewResource {
  ok: true
  content: Buffer
  headers: Record<string, string>
}

/**
 * What one request for the preview scheme is allowed to be answered with.
 *
 * Pure, so the two things that must never happen are testable without Electron:
 * an unknown or expired ticket answers nothing (never a fallback file), and a
 * ticket for one file cannot be spent on another name.
 */
export function previewResourceFor(
  url: string,
  store: PreviewTicketStore,
): PreviewResource | NativeActionRefusal {
  const asked = parsePreviewUrl(url)
  if (!asked) return refuse('not_found', 'the preview resource is not available')
  const ticket = store.contentFor(asked.token)
  if (!ticket) return refuse('not_found', 'the preview resource is not available')
  // One ticket, one name: the token authorizes *this* file and nothing else --
  // no directory, no sibling, no traversal (nothing to traverse to).
  if (asked.name !== ticket.name) {
    return refuse('not_found', 'the preview resource is not available')
  }
  return { ok: true, content: ticket.content, headers: previewHeaders(ticket.name, ticket.kind) }
}

/** One answer from the scheme handler, before it becomes an Electron `Response`. */
export interface PreviewAnswer {
  status: number
  headers: Record<string, string>
  body: Buffer
}

/**
 * The HTTP-shaped answer for one preview request.
 *
 * An unknown, expired or revoked ticket is a **404 with no body of the file**: a
 * handler that fell back to "serve the last thing" would be a handler that keeps
 * serving a file after its authorization is gone. The refusal text is the
 * generic one, so a probe cannot use the status to learn whether a file exists.
 */
export function answerPreviewRequest(url: string, store: PreviewTicketStore): PreviewAnswer {
  const resource = previewResourceFor(url, store)
  if (resource.ok === true) {
    return { status: 200, headers: resource.headers, body: resource.content }
  }
  return {
    status: 404,
    headers: {
      'Content-Type': 'text/plain; charset=utf-8',
      'Content-Security-Policy': "default-src 'none'; sandbox",
      'Cache-Control': 'no-store, max-age=0',
      'X-Content-Type-Options': 'nosniff',
    },
    body: Buffer.from(String(resource.message || 'not available'), 'utf8'),
  }
}

function extensionOf(name: string): string {
  const clean = String(name || '').split(/[\\/]/).pop() || ''
  const dot = clean.lastIndexOf('.')
  return dot > 0 ? clean.slice(dot + 1).toLowerCase() : ''
}

function defaultRandom(bytes: number): string {
  // Required lazily: the pure module stays importable where `node:crypto` is not
  // (a page-side bundler), and no token is minted outside the main process.
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const crypto = require('crypto') as typeof import('crypto')
  return crypto.randomBytes(bytes).toString('hex')
}

function refuse(code: string, message: string): NativeActionRefusal {
  return { ok: false, code, message }
}
