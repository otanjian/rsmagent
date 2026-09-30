// Remote host bridge: every decision the container makes about a caller.
//
// Change ``add-desktop-remote-web-workbench``, tasks 4.2-4.5. The container
// loads a *server* page in a WebContentsView, so "is this caller allowed to use
// a native capability?" cannot be answered by the page's own script, by a
// cookie, or by "the request came from our origin". The refusals that matter
// live here, as pure functions, so they can be driven for real by
// ``tests/test_desktop_remote_host.cjs`` instead of being asserted by grepping
// the source for a keyword:
//
//   * which bridge methods exist at all in phase 1 (contract ``bridge``);
//   * which (webContents, frame, document generation) may call them -- an
//     iframe, a popup, an old document or a same-origin non-shell page is not
//     the shell, and a URL that merely shares an origin proves nothing;
//   * where the top frame may navigate, and when an external link may be handed
//     to the system browser;
//   * which surfaces must never receive the bridge at all (preview documents,
//     generated HTML);
//   * how fast a permission dialog may be re-opened.

/** Bridge major this client implements (contracts ``bridge.version``). */
export const BRIDGE_VERSION = '1.0'

/** The narrow surface phase 1 exposes to the remote page. */
export const PHASE1_METHODS = [
  'getCapabilities',
  'suspendLocalContext',
  'saveArtifact',
  'openExternal',
  'onHostEvent',
] as const

/** Phase-2 additions for local-files (contracts ``bridge.methods``). */
export const PHASE2_METHODS = [
  'bindContext',
  'chooseWorkspace',
  'disconnectWorkspace',
] as const

/** Every method this build may expose once local-files is open. */
export const ALL_BRIDGE_METHODS = [...PHASE1_METHODS, ...PHASE2_METHODS] as const

//: An `openExternal` bridge call may carry at most this many bytes of inline
//: content once decoded. Larger files are fetched by the host through the
//: download policy (`downloads.ts`), never pushed through IPC.
export const SAVE_ARTIFACT_MAX_BYTES = 16777216

export type BridgeMethod = (typeof ALL_BRIDGE_METHODS)[number]

/**
 * Content paths whose answer is a file to save rather than a document to show.
 *
 * ``/preview`` and ``/uploads`` are documents (they carry markup of their own);
 * the API paths answer attachments. The distinction only decides which isolated
 * surface opens -- neither one may be the shell, and a document that arrives
 * with ``Content-Disposition: attachment`` is turned into a download by
 * Chromium itself, so the download policy still applies to it.
 */
const ATTACHMENT_PATHS = ['/api/file', '/api/workspace', '/api/knowledge',
  '/api/logs', '/api/artifacts']

/**
 * Paths that serve *content* (previews, uploads, downloads), never the shell.
 *
 * The attachment paths are part of the set on purpose: ``/api/knowledge`` and
 * ``/api/workspace`` answer files, so a document loaded from one of them must
 * never be treated as shell (no bridge, no shell navigation). ``/api/desktop``
 * is the control surface, which is equally never the shell.
 */
const CONTENT_PATHS = ['/preview', '/uploads', '/api/desktop', ...ATTACHMENT_PATHS]
/** Client-side route prefixes of the shipped shell (history-API routes). */
const SHELL_ROUTE_PREFIXES = ['/chat', '/agents', '/knowledge', '/tasks',
  '/skills', '/models', '/channels', '/scheduler', '/admin', '/account',
  '/platform', '/tenant', '/ops', '/settings']

export interface FrameIdentity {
  /** ``webContents.id`` of the container that owns the frame. */
  webContentsId: number
  /** ``frameProcessId``/``frameRoutingId`` of the *shell* main frame. */
  frameProcessId: number
  frameRoutingId: number
}

export interface SenderDescription extends FrameIdentity {
  /** True only for the frame that is the document of the view itself. */
  isMainFrame: boolean
  /** True when this frame has a parent (an iframe of the shell document). */
  hasParentFrame: boolean
  /** The frame's current URL. */
  url: string
  /** The generation stamped into the document that is calling. */
  generation: number
}

export interface SenderContext {
  /** The container's only webContents. */
  webContents: FrameIdentity
  /** The registered shell document's frame ids. */
  shellFrame: FrameIdentity
  /** Exact backend origin the container was loaded from. */
  origin: string
  /** Monotonic generation of the *current* shell document. */
  generation: number
  /** Entry paths the server advertised for this shell. */
  entryPaths: string[]
}

export type Verdict = { ok: true } | { ok: false; code: string; message: string }

function refuse(code: string, message: string): Verdict {
  return { ok: false, code, message }
}

/** Parse a URL, or null. Never throws: callers pass untrusted strings. */
export function safeUrl(raw: string): URL | null {
  try {
    const parsed = new URL(raw || '')
    return parsed.protocol ? parsed : null
  } catch {
    return null
  }
}

/** True when the two spellings are the same origin (scheme, host and port). */
export function sameOrigin(a: string, b: string): boolean {
  const left = safeUrl(a)
  const right = safeUrl(b)
  return !!left && !!right && left.origin === right.origin
}

/** True for a path that serves content, which must never be the shell. */
export function isContentPath(pathname: string): boolean {
  const path = pathname || '/'
  return CONTENT_PATHS.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))
}

/**
 * Content paths whose answer is a file to save rather than a document to show.
 *
 * ``/preview`` and ``/uploads`` are documents (they carry the page's own
 * markup); the API paths answer attachments. ``isAttachmentPath`` is the
 * predicate; this list is what ``CONTENT_PATHS`` is derived from.
 */
export function isAttachmentPath(pathname: string): boolean {
  const path = pathname || '/'
  return ATTACHMENT_PATHS.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))
}

/**
 * Whether an IPC call may act on the native boundary.
 *
 * The order of these checks is the order of the attacks: a call from another
 * webContents is not ours at all; a child frame or a popup is a different
 * document that happens to live in our process; a frame id that no longer
 * matches means the document was replaced (a navigation we did not stamp); and
 * a URL that only *shares our origin* is exactly the case A08 forbids, because
 * the same origin also serves previews, uploads and the JSON API.
 */
export function checkSender(sender: SenderDescription, context: SenderContext): Verdict {
  if (sender.webContentsId !== context.webContents.webContentsId) {
    return refuse('permission_denied', 'the call came from another webContents')
  }
  if (!sender.isMainFrame || sender.hasParentFrame) {
    return refuse('permission_denied', 'the call came from a child frame')
  }
  if (sender.frameProcessId !== context.shellFrame.frameProcessId
    || sender.frameRoutingId !== context.shellFrame.frameRoutingId) {
    return refuse('stale_context', 'the calling document is not the registered shell document')
  }
  if (sender.generation !== context.generation) {
    return refuse('stale_context', 'the calling document belongs to an earlier generation')
  }
  if (!sameOrigin(sender.url, context.origin)) {
    return refuse('permission_denied', 'the calling document is not on the backend origin')
  }
  const parsed = safeUrl(sender.url)
  if (!parsed) return refuse('permission_denied', 'the calling document has no usable URL')
  if (isContentPath(parsed.pathname)) {
    return refuse('permission_denied', 'the calling document is a content surface, not the shell')
  }
  return { ok: true }
}

/** A bridge call, as the preload relays it. */
export interface BridgeCall {
  method: string
  /** Untrusted: every method validates its own shape below. */
  params?: Record<string, unknown>
}

const MAX_TEXT = 4096

function requireString(params: Record<string, unknown>, key: string, max = MAX_TEXT): string | null {
  const value = params[key]
  if (typeof value !== 'string' || !value || value.length > max) return null
  return value
}

/** True for a non-null, non-array object. */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * Validate one bridge call.
 *
 * Phase 1 is deliberately tiny, and each method's shape is checked here rather
 * than in the preload: a page that hands ``saveArtifact`` a URL, a ``file://``
 * path or a 10 MiB string must be refused by the same code path that a
 * well-formed call is accepted by.
 */
export function checkBridgeCall(call: BridgeCall): Verdict {
  const method = call?.method
  if (typeof method !== 'string' || !(ALL_BRIDGE_METHODS as readonly string[]).includes(method)) {
    return refuse('feature_unavailable', 'the bridge method is not part of this bridge version')
  }
  const params = (call.params || {}) as Record<string, unknown>
  switch (method as BridgeMethod) {
    case 'getCapabilities':
      return { ok: true }
    case 'suspendLocalContext':
      if (params.reason !== undefined && typeof params.reason !== 'string') {
        return refuse('invalid_request', 'suspendLocalContext requires a textual reason')
      }
      return { ok: true }
    case 'saveArtifact': {
      const name = requireString(params, 'name', 255)
      if (!name) return refuse('invalid_request', 'saveArtifact requires a file name')
      if (/[/\\]/.test(name) || name === '.' || name === '..') {
        return refuse('invalid_path', 'saveArtifact must be given a plain file name')
      }
      const content = params.content
      if (typeof content !== 'string') {
        return refuse('invalid_request', 'saveArtifact requires inline content')
      }
      // Base64 expands 3 bytes to 4 characters; the bound is checked on the
      // decoded size so a page cannot push an arbitrarily large buffer through
      // IPC by claiming it is "inline".
      const decoded = Math.floor((content.length * 3) / 4)
      if (decoded > SAVE_ARTIFACT_MAX_BYTES) {
        return refuse('limit_exceeded', `inline content exceeds ${SAVE_ARTIFACT_MAX_BYTES} bytes`)
      }
      return { ok: true }
    }
    case 'openExternal': {
      const url = requireString(params, 'url', 2048)
      if (!url) return refuse('invalid_request', 'openExternal requires a URL')
      return checkExternalUrl(url)
    }
    case 'onHostEvent':
      return { ok: true }
    case 'chooseWorkspace':
      // Scope fields are validated in the dispatcher against the live grant
      // registry; the shape check only refuses non-objects.
      if (params.scope !== undefined && !isPlainObject(params.scope)) {
        return refuse('invalid_request', 'chooseWorkspace.scope must be an object')
      }
      return { ok: true }
    case 'disconnectWorkspace':
      return { ok: true }
    case 'bindContext': {
      // The page proposes *what* to bind to; the main process decides *whether*
      // it may (document generation, scope, live grant) and resolves the server
      // ids itself. The generation is not a parameter: the preload stamps it
      // from the handshake, so a page cannot claim one.
      if (!isPlainObject(params.scope)) {
        return refuse('invalid_request', 'bindContext.scope must be an object')
      }
      for (const key of ['serverId', 'userId', 'tenantId', 'deviceId'] as const) {
        if (typeof params.scope[key] !== 'string' || !params.scope[key]) {
          return refuse('invalid_request', `bindContext.scope.${key} is required`)
        }
      }
      for (const key of ['installationId', 'label', 'agentId',
        'businessSessionId', 'contextNonce'] as const) {
        if (typeof params[key] !== 'string' || !params[key]) {
          return refuse('invalid_request', `bindContext.${key} is required`)
        }
      }
      return { ok: true }
    }
    default:
      return refuse('feature_unavailable', 'the bridge method is not part of this bridge version')
  }
}

/** Only a plain ``http(s)`` URL may leave the application. */
export function checkExternalUrl(raw: string): Verdict {
  const parsed = safeUrl(raw)
  if (!parsed) return refuse('invalid_request', 'the external target is not a URL')
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return refuse('unsafe_path', 'only http(s) links may be opened externally')
  }
  if (parsed.username || parsed.password) {
    return refuse('unsafe_path', 'the external target carries credentials')
  }
  return { ok: true }
}

/**
 * Whether a top-frame navigation may proceed.
 *
 * The container keeps the shell on its own origin and inside the shell's route
 * space. Everything else -- another site, a content path the page could be
 * talked into showing as the shell, a ``file:`` or ``javascript:`` target --
 * is refused, and the caller decides whether a *user gesture* turns it into an
 * external open instead.
 */
export type NavigationVerdict =
  | { ok: true }
  | {
      ok: false
      code: string
      message: string
      /** Hand this target to the system browser (an off-origin web target). */
      external?: boolean
      /** Show this target in the isolated content window, never in the shell. */
      content?: 'document' | 'attachment'
    }

/**
 * Whether a top-frame navigation may replace the shell document.
 *
 * The shell is a fixed set of routes; everything else is refused *as a shell
 * document*. A same-origin content path is not merely refused, though -- it is
 * classified, because the wiring has to do something useful with it: a preview
 * or an attachment belongs in the isolated content window (which has no
 * bridge), and a download path belongs to the download policy. Refusing it
 * without saying which one would leave the pages that open files dead in the
 * container while behaving correctly in a browser.
 */
export function checkTopFrameNavigation(
  raw: string,
  context: { origin: string; entryPaths: string[] },
): NavigationVerdict {
  const parsed = safeUrl(raw)
  if (!parsed) return refuse('invalid_request', 'the navigation target is not a URL')
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return { ok: false, code: 'unsafe_path', message: 'the navigation target is not a web URL' }
  }
  if (!sameOrigin(raw, context.origin)) {
    return { ok: false, code: 'unsafe_path', message: 'the navigation leaves the backend origin', external: true }
  }
  if (isContentPath(parsed.pathname)) {
    const content = isAttachmentPath(parsed.pathname) ? 'attachment' : 'document'
    return {
      ok: false,
      code: 'unsafe_path',
      message: 'a content path may not replace the shell document',
      content,
    }
  }
  if (isShellDocument(parsed.pathname, context.entryPaths)) return { ok: true }
  return { ok: false, code: 'unsafe_path', message: 'the target is not a registered shell document' }
}

/**
 * Whether a pathname is a shell document of this application.
 *
 * Both the server-registered entry paths and the shipped client-side routes
 * count: the former is what the server says can be loaded as the shell, the
 * latter is where the running shell navigates itself. A path that is *only*
 * same-origin (an uploaded file, a rendered preview, the JSON API) is not.
 */
export function isShellDocument(pathname: string, entryPaths: string[]): boolean {
  const path = pathname || '/'
  if (isContentPath(path)) return false
  // The console root is the shell itself even when the server does not list it.
  if (path === '/') return true
  for (const entry of entryPaths || []) {
    const bare = entry.replace(/\/+$/, '')
    if (!bare || bare === '/') continue
    if (path === bare || path.startsWith(`${bare}/`)) return true
  }
  return SHELL_ROUTE_PREFIXES.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))
}

export type OpenRoute =
  | { ok: true; route: 'system-browser'; url: string }
  | { ok: true; route: 'content-window'; url: string; kind: 'document' | 'attachment' }
  | { ok: false; code: string; message: string }

/**
 * Where a URL the page asked to *open* should actually go.
 *
 * ``window.open`` (and the bridge's ``openExternal``) is one question with
 * three answers in a container, and the page must not have to know which:
 *
 *   * a foreign http(s) link is the system browser -- never a window this
 *     process owns, because that window would share the guest's session;
 *   * a same-origin *content* path (a preview, an uploaded document, an
 *     attachment) belongs in the isolated content window, which has the
 *     partition's cookies but no bridge;
 *   * a same-origin non-content path is refused. Opening the shell again --
 *     or any other same-origin page -- outside the stamped document is exactly
 *     the "same-origin page borrows the session" case A08 forbids, and a
 *     browser tab pointed at it would be a second, unpaired session.
 */
export function routeOpen(
  raw: string,
  context: { origin: string; entryPaths: string[] },
): OpenRoute {
  const parsed = safeUrl(raw)
  if (!parsed) {
    return { ok: false, code: 'invalid_request', message: 'the target is not a URL' }
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return { ok: false, code: 'unsafe_path', message: 'the target is not a web URL' }
  }
  if (parsed.username || parsed.password) {
    return { ok: false, code: 'unsafe_path', message: 'the target carries credentials' }
  }
  if (!sameOrigin(raw, context.origin)) {
    return { ok: true, route: 'system-browser', url: parsed.toString() }
  }
  if (isContentPath(parsed.pathname)) {
    return {
      ok: true,
      route: 'content-window',
      url: parsed.toString(),
      kind: isAttachmentPath(parsed.pathname) ? 'attachment' : 'document',
    }
  }
  return {
    ok: false,
    code: 'unsafe_path',
    message: 'a same-origin page is not an external link',
  }
}

/**
 * Where an external link may be handed to the system browser.
 *
 * A window opened by the page needs an explicit user gesture; a script that
 * opens windows in a loop must not be able to spray browser tabs (or dialogs).
 */
export function checkOpenExternal(raw: string, userGesture: boolean): Verdict {
  if (!userGesture) {
    return refuse('permission_denied', 'opening an external link requires a user gesture')
  }
  return checkExternalUrl(raw)
}

/** A small sliding-window limiter for permission dialogs (task 4.5). */
export class RateLimiter {
  private readonly hits: number[] = []

  constructor(private readonly limit: number, private readonly windowMs: number) {}

  /** True when the call is allowed; a refusal consumes nothing. */
  allow(now: number = Date.now()): boolean {
    const floor = now - this.windowMs
    while (this.hits.length && this.hits[0] <= floor) this.hits.shift()
    if (this.hits.length >= this.limit) return false
    this.hits.push(now)
    return true
  }

  get pending(): number {
    return this.hits.length
  }
}

/** Media and device permission types the container answers without asking the OS. */
export const DENIED_PERMISSIONS = ['media', 'geolocation', 'notifications', 'midi',
  'midiSysex', 'hid', 'serial', 'usb', 'idle-detection', 'clipboard-read',
  'clipboard-sanitized-write']

/**
 * The container's answer to a permission request.
 *
 * Phase 1 grants none of them: a remote page that asks for the microphone, the
 * camera, the location or the serial port gets a refusal, and the *shell* (which
 * may notify or record through its own native surface) is not affected. A
 * permission the list does not name is refused too -- the list documents what
 * was considered, it is not an allow-list.
 */
export function permissionVerdict(_permission: string): 'deny' {
  return 'deny'
}

/**
 * Whether a view created for this URL may receive the bridge preload.
 *
 * Preview and generated HTML are the classic escape hatch: they are same-origin
 * with the shell, so they can reach the shell's window if they are ever loaded
 * in the same document tree. They are therefore loaded in their own view with
 * no preload and no bridge -- the *path*, not the origin, decides.
 */
export function needsBridgePreload(rawUrl: string, options: { origin: string }): boolean {
  const parsed = safeUrl(rawUrl)
  if (!parsed) return false
  if (!sameOrigin(rawUrl, options.origin)) return false
  return !isContentPath(parsed.pathname)
}
