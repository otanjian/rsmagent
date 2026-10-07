import { app, BrowserWindow, ipcMain, session as electronSession, shell, type IpcMainInvokeEvent } from 'electron'
import { createHash, randomBytes, timingSafeEqual } from 'crypto'
import * as http from 'http'
import * as path from 'path'
import { fileURLToPath } from 'url'
import type { AddressInfo } from 'net'
import { parseServerOrigin } from './remote/profiles'
import { AssetProxy, type AssetTarget, type UpstreamReply } from './asset-proxy'
import {
  WEB_SESSION_COOKIE, WEB_SESSION_MAJOR, checkBootstrapReply, isValidInstanceId,
  partitionName,
} from './remote/web-session'

// The Desktop auth/transport broker (design D8, task group 8).
//
// One authoritative identity state
// --------------------------------
// The main process is the only place a session credential exists. It holds:
// the native ``AuthSession`` Bearer, the desensitized self projection, the
// currently selected tenant and a monotonic ``epoch`` that changes on every
// account/tenant switch. The renderer receives only that projection -- never a
// token, never a hash, never a header it could replay. Everything the app does
// travels through :func:`requestBusiness`, which attaches the Bearer and the
// current tenant itself, so a newly added business method cannot forget them
// (task 10.2's "one seam" requirement).
//
// Why authorization-code + PKCE instead of the login response
// ----------------------------------------------------------
// The old shell read a reusable Bearer token out of ``POST /auth/login`` and
// kept it in ``localStorage``; a ``file://`` renderer has to, because cookies
// are unreliable there and ``EventSource`` cannot set headers. That made one
// readable HTTP response an authorization grant for a long-lived native session
// and put the token in the renderer and in query strings. Now:
//
// 1. the main process generates a PKCE verifier, a ``state`` and one callback
//    path, and binds its loopback listener *before* the browser is opened;
// 2. the system browser signs the user in and shows an explicit consent page
//    (``/auth/desktop/authorize``);
// 3. the browser redirects to the loopback callback with a 60-second,
//    single-use code;
// 4. the main process trades the code + verifier at ``/auth/desktop/token`` for
//    an independent native ``AuthSession``.
//
// The code proves the caller holds the verifier; neither PKCE nor the public
// client id proves anything about the client binary, and this module claims no
// such thing. A User-Agent, a self-declared "desktop" flag or an existing
// browser Cookie never mints a session on its own.
//
// Hard rules enforced here
// ------------------------
// * No secret is ever logged, persisted, returned to the renderer, or put in a
//   URL. The token lives in this module's memory and nowhere else.
// * The backend origin is owned by the main process (the port the backend
//   manager actually bound). The renderer cannot pass, override or discover it.
// * The loopback listener accepts one GET, at the exact path it minted, from
//   the literal loopback address, carrying the exact ``state``; anything else
//   closes it without a session.
// * Credentialed redirects are refused (``redirect: 'error'``), so a backend
//   answer that tries to bounce the Bearer elsewhere fails instead.
// * Tenant switch bumps the epoch, aborts in-flight work, drops the previous
//   epoch's asset ids and makes late responses ``stale_context`` -- a stale
//   write is never re-sent under the new tenant.
// * Logout revokes the server session first. If revocation fails the app stops
//   business requests and says so, rather than claiming the session is gone.

/** The fixed public client id (design D8). Public by definition; a label. */
const CLIENT_ID = 'cowagent-desktop'

/** How long the whole browser round trip may take before it is abandoned. */
const AUTHORIZE_TIMEOUT_MS = 5 * 60 * 1000

/** Business request timeout; a stalled backend must not wedge the UI. */
const REQUEST_TIMEOUT_MS = 60 * 1000

/** Registered local backends only: the literal loopback forms. */
const LOOPBACK_HOST = '127.0.0.1'

/** Auth endpoints the generic relay and the renderer must never reach. */
const NATIVE_AUTH_PATHS = ['/auth/login', '/auth/desktop/', '/auth/logout', '/auth/password']

/**
 * Paths the broker itself owns: the renderer asks for them through a dedicated
 * IPC channel (so the credential handling stays in one auditable place) and may
 * never smuggle them through the generic business request.
 */
function isBrokerOwnedPath(path: string): boolean {
  return NATIVE_AUTH_PATHS.some((prefix) => path === prefix || path.startsWith(prefix))
}

/** Personal/platform reads that must not carry a tenant selection. */
function wantsTenantHeader(path: string): boolean {
  // Mirrors the Web console's rule (channel/web/static/js/console.js): the
  // selected tenant is attached to ``/api/**`` and to the non-/api transports
  // that resolve their tenant from the selection. ``/api/auth/**`` is the
  // account surface and never carries one.
  if (path.startsWith('/api/auth/')) return false
  if (path.startsWith('/api/')) return true
  return /^\/(message|stream|poll|cancel|upload)\b/.test(path)
}

function b64url(raw: Buffer): string {
  return raw.toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function s256(verifier: string): string {
  return b64url(createHash('sha256').update(verifier, 'ascii').digest())
}

/** One signing-in browser round trip: the pending PKCE transaction. */
interface AuthorizationResult {
  code?: string
  error?: string
  serverOrigin?: string
}

interface PendingAuthorization {
  verifier: string
  state: string
  /** Exact registered loopback callback this attempt will accept. */
  redirectUri: string
  server: http.Server
  port: number
  settle: (result: AuthorizationResult) => void
}

/** The desensitized projection the renderer is allowed to see. */
export interface BrokerSession {
  userId: string
  username: string
  displayName: string
  isPlatformAdmin: boolean
  mustChangePassword: boolean
  tenants: Array<{ id: string; code: string; name: string }>
  tenantId: string | null
  /** Incremented on every account/tenant switch; renderers compare it. */
  epoch: number
}

export interface BrokerStatus {
  /** Non-empty when the app must stop touching the backend (logout failure). */
  blockedReason: string
  session: BrokerSession | null
  authRequired: boolean
  identityMode: string
}

interface SessionState {
  token: string
  origin: string
  epoch: number
  projection: BrokerSession
}

/** Everything the main process keeps about the current native context. */
interface BrokerState {
  backendOrigin: string
  session: SessionState | null
  /** True after a failed server-side revocation: business requests stop. */
  blockedReason: string
  identityMode: string
  authRequired: boolean
  probeFailed: boolean
  pending: PendingAuthorization | null
  inFlight: Set<AbortController>
  proxy: AssetProxy | null
}

const state: BrokerState = {
  backendOrigin: '',
  session: null,
  blockedReason: '',
  identityMode: '',
  authRequired: true,
  probeFailed: false,
  pending: null,
  inFlight: new Set(),
  proxy: null,
}
let authorizationGeneration = 0

// --------------------------------------------------------------------------- //
// Backend origin (owned by the main process)
// --------------------------------------------------------------------------- //

/**
 * The origin of the *bundled* backend this process started, kept apart from the
 * active one.
 *
 * ``state.backendOrigin`` is the single origin every credentialed request is
 * aimed at, and which of the two candidates it is depends on the mode: the
 * bundled backend in local mode, the active server in remote mode. Remembering
 * the local one separately is what lets the mode be switched back without a
 * restart -- and, more to the point, what keeps an unrelated config write from
 * discarding it. Adding a server to the list while local used to clear the
 * origin outright, which left a signed-in window with no transport at all.
 */
let localBackendOrigin = ''

/**
 * The bundled backend announced itself, or went away.
 *
 * Records the origin *and* installs it, which is what local mode wants. Remote
 * mode is unaffected: ``syncBrokerOrigin`` runs before the bundled backend is
 * ever started, and in remote mode it never is.
 */
export function announceLocalBackendOrigin(origin: string): void {
  localBackendOrigin = (origin || '').replace(/\/+$/, '')
  setBackendOrigin(localBackendOrigin)
}

/** The bundled backend's origin; empty when it is not running. */
export function getLocalBackendOrigin(): string {
  return localBackendOrigin
}

/**
 * The native bearer, for the one caller that cannot use ``fetch``.
 *
 * Change ``fix-desktop-local-context-and-tool-calls`` (task 2.4): the device
 * gateway is a WebSocket, so its handshake has to carry the credential as a
 * header rather than going through :func:`requestBusiness`. That makes this
 * accessor an exception, and it is kept as narrow as the rest of the broker:
 * main-process only (never an IPC handler), read fresh per attempt so a
 * reconnect after a re-auth uses the current session, and ``null`` -- never a
 * stale token -- when there is no session.
 */
export function nativeBearer(): string | null {
  return state.session?.token || null
}

/** The origin the native session belongs to, or '' when there is none. */
export function nativeSessionOrigin(): string {
  return state.session?.origin || ''
}

/**
 * Publish the backend the shell is actually talking to.
 *
 * The port is discovered by the Python backend manager (it may fall back when
 * the preferred port is unbindable), so it is pushed here by the main process
 * rather than accepted from a renderer argument. A change of origin invalidates
 * the previous context: the old session belonged to another server and must not
 * be replayed against the new one.
 */
export function setBackendOrigin(origin: string): void {
  const next = (origin || '').replace(/\/+$/, '')
  if (next === state.backendOrigin) return
  if (state.session) {
    state.session = null
    state.blockedReason = ''
    abortInFlight()
    state.proxy?.clear()
  }
  // A child cookie belongs to the server it was minted by; a change of origin
  // destroys the partition rather than leaving a cookie for the old one around.
  void clearWebChildSessions()
  state.backendOrigin = next
  state.probeFailed = false
}

export function getBackendOrigin(): string {
  return state.backendOrigin
}

/** Register the loopback asset proxy that carries header-less transports. */
export function setupAssetProxy(): void {
  if (state.proxy) return
  state.proxy = new AssetProxy(fetchAssetUpstream)
  // Bind eagerly: the synchronous asset-URL channel used by JSX `src=` and
  // `window.open` cannot await a listen, so the port is ready before the first
  // request arrives. A failure here only degrades asset URLs, never the
  // credential handling, and the async channel would report it.
  void state.proxy.start().catch(() => undefined)
}

// --------------------------------------------------------------------------- //
// Low-level credentialed transport
// --------------------------------------------------------------------------- //

interface RawReply {
  status: number
  statusText: string
  contentType: string
  body: string
  /**
   * Every ``Set-Cookie`` the answer carried. Only the Web child bootstrap reads
   * this; it is never forwarded to a renderer or written to a log.
   */
  setCookies: string[]
}

/** What the broker will accept as a body from the renderer. */
export interface BrokerForm {
  fields: Array<{ name: string; value: string }>
  files: Array<{ name: string; filename: string; contentType: string; bytes: ArrayBuffer }>
}

export interface BusinessRequest {
  path: string
  method?: string
  /** JSON body, already serialized by the caller. */
  body?: string
  /** Multipart body, reassembled here so the bearer stays in this process. */
  form?: BrokerForm
}

interface SendOptions {
  method: string
  body?: string | Uint8Array
  contentType?: string
  /** Attach the selected tenant when one is set (business transports). */
  withTenant: boolean
  /** Attach the native bearer (everything but the probe/login calls). */
  withToken: boolean
  signal?: AbortSignal
}

/** One backend request, credentialed here and nowhere else. */
async function send(path: string, options: SendOptions): Promise<RawReply> {
  if (!state.backendOrigin) throw new BrokerError('backend_unavailable', 'backend is not ready')
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (options.contentType) headers['Content-Type'] = options.contentType
  if (options.withToken && state.session) {
    headers['Authorization'] = `Bearer ${state.session.token}`
    if (options.withTenant && state.session.projection.tenantId && wantsTenantHeader(path)) {
      headers['X-Tenant-ID'] = state.session.projection.tenantId
    }
  }
  let res: Response
  try {
    res = await fetch(`${state.backendOrigin}${path}`, {
      method: options.method,
      headers,
      body: options.body,
      signal: options.signal,
      // A credentialed redirect is refused outright: the bearer must never be
      // replayed to a host the answer chooses.
      redirect: 'error',
    })
  } catch (e) {
    throw new BrokerError('network_error', networkMessage(e))
  }
  const body = await res.text()
  // ``getSetCookie`` is the only way to read several Set-Cookie headers apart;
  // the fallback keeps older runtimes readable (single cookie, joined value).
  const setCookies =
    typeof (res.headers as { getSetCookie?: () => string[] }).getSetCookie === 'function'
      ? (res.headers as { getSetCookie: () => string[] }).getSetCookie()
      : (res.headers.get('set-cookie') ? [res.headers.get('set-cookie') as string] : [])
  return {
    status: res.status,
    statusText: res.statusText,
    contentType: res.headers.get('content-type') || '',
    body,
    setCookies,
  }
}

function networkMessage(e: unknown): string {
  const text = e instanceof Error ? e.message : String(e)
  // fetch() failures can echo the request URL; the callback URL carries the
  // one-time code, so it is never surfaced or logged.
  if (/redirect/i.test(text)) return 'the backend tried to redirect an authenticated request'
  return 'could not reach the local service'
}

/** A refusal the renderer maps to a concrete state (never empty data). */
export class BrokerError extends Error {
  constructor(public readonly code: string, message: string, public readonly status = 0) {
    super(message)
  }
}

function abortInFlight(): void {
  for (const controller of state.inFlight) controller.abort()
  state.inFlight.clear()
}

// --------------------------------------------------------------------------- //
// Interactive authorization (system browser + loopback callback)
// --------------------------------------------------------------------------- //

/** Start the one-shot loopback listener the redirect must come back to. */
function listenForCallback(): Promise<{ server: http.Server; port: number }> {
  return new Promise((resolve, reject) => {
    const server = http.createServer()
    server.on('clientError', (_err, socket) => socket.destroy())
    server.once('error', reject)
    server.listen(0, LOOPBACK_HOST, () => {
      server.removeListener('error', reject)
      resolve({ server, port: (server.address() as AddressInfo).port })
    })
  })
}

function callbackPage(title: string, message: string): string {
  return (
    '<!doctype html><html><head><meta charset="utf-8">' +
    '<meta name="referrer" content="no-referrer">' +
    `<title>${title}</title></head><body style="font-family:system-ui;padding:3rem">` +
    `<h1>${title}</h1><p>${message}</p></body></html>`
  )
}

/**
 * Run one authorization: listener first, then the system browser, then the
 * exchange. Resolves once the native session is established.
 */
async function authorizeInteractive(generation: number): Promise<BrokerSession | { serverOrigin: string }> {
  if (!state.backendOrigin) throw new BrokerError('backend_unavailable', 'backend is not ready')
  const authorizationOrigin = state.backendOrigin

  const verifier = b64url(randomBytes(32))
  const challenge = s256(verifier)
  const stateToken = b64url(randomBytes(24))
  const callbackPath = `/callback/${b64url(randomBytes(12))}`
  const { server, port } = await listenForCallback()
  if (generation !== authorizationGeneration) {
    server.close()
    throw new BrokerError('authorization_cancelled', 'authorization was cancelled')
  }
  const redirectUri = `http://${LOOPBACK_HOST}:${port}${callbackPath}`

  const codePromise = new Promise<AuthorizationResult>((resolve) => {
    state.pending = {
      verifier, state: stateToken, redirectUri, server, port, settle: resolve,
    }
  })
  const attempt = state.pending!

  server.on('request', (req, res) => {
    const pending = state.pending
    const url = new URL(req.url || '/', `http://${LOOPBACK_HOST}:${port}`)
    if (req.method !== 'GET' || pending !== attempt || url.pathname !== callbackPath) {
      res.writeHead(404, { 'Content-Type': 'text/plain' })
      res.end('not found')
      return
    }
    // The callback carries the code and the state; a mismatch means somebody
    // else's attempt (or a forged one) and must not complete this transaction.
    const gotState = url.searchParams.get('state') || ''
    const expected = Buffer.from(pending.state)
    const actual = Buffer.from(gotState)
    if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) {
      res.writeHead(400, { 'Content-Type': 'text/html; charset=utf-8' })
      res.end(callbackPage('Authorization failed', 'The request did not match this app. Nothing was authorized.'))
      return
    }
    const code = url.searchParams.get('code') || ''
    const error = url.searchParams.get('error') || ''
    let serverOrigin: string | undefined
    if (error === 'server_changed') {
      const parsed = parseServerOrigin(url.searchParams.get('server_origin'))
      if (code || !parsed.ok) {
        res.writeHead(400, { 'Content-Type': 'text/html; charset=utf-8' })
        res.end(callbackPage('服务器地址无效', '请返回授权页，填写有效的 HTTPS 服务器地址。'))
        return
      }
      serverOrigin = parsed.origin
    }
    res.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
      'Referrer-Policy': 'no-referrer',
    })
    res.end(
      serverOrigin
        ? callbackPage('正在切换服务器', '桌面端将打开新服务器的登录页面，请在新页面登录并确认授权。')
        : code
        ? callbackPage('Authorization complete', 'You can close this tab and return to the app.')
        : callbackPage('Authorization cancelled', 'No access was granted.'),
    )
    pending.settle({ code: code || undefined, error: error || undefined, serverOrigin })
  })

  const authorizeUrl =
    `${authorizationOrigin}/auth/desktop/authorize` +
    `?client_id=${encodeURIComponent(CLIENT_ID)}` +
    `&redirect_uri=${encodeURIComponent(redirectUri)}` +
    `&code_challenge=${encodeURIComponent(challenge)}` +
    '&code_challenge_method=S256' +
    `&state=${encodeURIComponent(stateToken)}`

  const timer = setTimeout(() => {
    attempt.settle({ error: 'timeout' })
  }, AUTHORIZE_TIMEOUT_MS)

  try {
    await shell.openExternal(authorizeUrl)
    const result = await codePromise
    if (result.error === 'cancelled' || generation !== authorizationGeneration) {
      throw new BrokerError('authorization_cancelled', 'authorization was cancelled')
    }
    if (state.pending !== attempt || state.backendOrigin !== authorizationOrigin) {
      throw new BrokerError('authorization_server_changed', 'the server changed; start sign-in again')
    }
    if (result.serverOrigin) return { serverOrigin: result.serverOrigin }
    if (!result.code) {
      throw new BrokerError(result.error === 'timeout' ? 'authorization_timeout' : 'authorization_cancelled',
        result.error === 'timeout'
          ? 'the browser authorization timed out'
          : 'authorization was not granted')
    }
    return await exchangeCode(result.code, verifier, redirectUri, authorizationOrigin, generation)
  } finally {
    clearTimeout(timer)
    if (state.pending === attempt) closePendingAuthorization()
  }
}

/** Close the listener and wipe the temporary secrets of this attempt. */
function closePendingAuthorization(): void {
  const pending = state.pending
  state.pending = null
  if (!pending) return
  try {
    pending.server.close()
  } catch {
    /* already closed */
  }
}

export function cancelPendingAuthorization(): void {
  authorizationGeneration++
  const pending = state.pending
  if (!pending) return
  pending.settle({ error: 'cancelled' })
  closePendingAuthorization()
}

/** Trade the code + verifier for an independent native session. */
async function exchangeCode(code: string, verifier: string, redirectUri: string,
                            origin: string, generation: number): Promise<BrokerSession> {
  const reply = await send('/auth/desktop/token', {
    method: 'POST',
    contentType: 'application/json',
    withTenant: false,
    withToken: false,
    body: JSON.stringify({
      code,
      code_verifier: verifier,
      client_id: CLIENT_ID,
      redirect_uri: redirectUri,
    }),
  })
  let parsed: { status?: string; token?: string; message?: string; code?: string } = {}
  try {
    parsed = JSON.parse(reply.body)
  } catch {
    /* non-JSON answer */
  }
  if (reply.status !== 200 || !parsed.token) {
    throw new BrokerError(parsed.code || 'exchange_failed',
      parsed.message || 'the authorization code could not be exchanged')
  }
  const token = parsed.token
  const cancelled = () => generation !== authorizationGeneration || state.backendOrigin !== origin
  const revokeCancelled = async () => {
    // The code may already have been consumed when Cancel was clicked. Revoke
    // that late session on its own server; never publish it or use a new origin.
    await fetch(`${origin}/auth/logout`, {
      method: 'POST', headers: { Authorization: `Bearer ${token}` },
      redirect: 'error', signal: AbortSignal.timeout(5000),
    }).catch(() => undefined)
    throw new BrokerError('authorization_cancelled', 'authorization was cancelled')
  }
  if (cancelled()) return revokeCancelled()
  const projection = await loadProjection(token, origin)
  if (cancelled()) return revokeCancelled()
  state.session = { token, origin, epoch: nextEpoch(), projection }
  // The token is deliberately not stored anywhere else: no cache file, no
  // localStorage, no disk. Application restart requires a new authorization.
  return projection
}

// --------------------------------------------------------------------------- //
// Web child session for the remote container (task 3.7)
// --------------------------------------------------------------------------- //

/**
 * A bootstrapped child session, as much as the main process may know.
 *
 * ``partition`` is the answer the container needs: it must load the page into
 * *this* in-memory partition so the page's requests carry the paired Cookie.
 * No token is part of this value, so returning it through the ordinary
 * (already sender-guarded) IPC channel would not leak a credential -- the
 * bootstrap call itself is still kept off the renderer's surface.
 */
export interface WebChildSession {
  /** In-memory Electron partition the container must be created in. */
  partition: string
  linkId: string
  expiresAt: number
  webProtocol: number
}

/** Partitions created by this process. Nothing here is ever persisted. */
const childPartitions = new Set<string>()

/**
 * Ask the server for the Web child session of the current native parent.
 *
 * Every check that can be made without Electron lives in
 * ``remote/web-session.ts`` (exact-origin refusal, cookie attributes, "the
 * secret is in the Cookie and nowhere else"); this function adds the two things
 * only the main process can do -- install the cookie into a fresh in-memory
 * partition, and hand back a descriptor that carries no secret.
 *
 * The bootstrap id is generated here with 192 bits of entropy and never reused:
 * the server treats a repeat as ``bootstrap_consumed`` rather than minting a
 * second child.
 */
export async function bootstrapWebSession(instanceId: string): Promise<WebChildSession> {
  const native = state.session
  if (!native) throw new BrokerError('unauthorized', 'not signed in', 401)
  if (!state.backendOrigin) throw new BrokerError('backend_unavailable', 'backend is not ready')
  if (native.origin !== state.backendOrigin) {
    // The session was authorized against another server; the server would
    // compare its registered origin and refuse, so do not even ask.
    throw new BrokerError('unsupported_origin',
      'the session was authorized against another server', 400)
  }
  if (!isValidInstanceId(instanceId)) {
    throw new BrokerError('invalid_request', 'invalid instance id', 400)
  }
  const reply = await send('/auth/desktop/web-session', {
    method: 'POST',
    contentType: 'application/json',
    withTenant: false,
    withToken: true,
    body: JSON.stringify({
      bootstrap_id: b64url(randomBytes(24)),
      instance_id: instanceId,
      web_protocol: WEB_SESSION_MAJOR,
    }),
  })
  const checked = checkBootstrapReply(reply, native.token)
  if (!checked.ok) throw new BrokerError(checked.code, checked.message, checked.status)
  const partition = partitionName(instanceId)
  await installChildCookie(partition, checked.cookie.value)
  childPartitions.add(partition)
  return {
    partition,
    linkId: checked.linkId,
    expiresAt: checked.expiresAt,
    webProtocol: checked.webProtocol,
  }
}

/**
 * Install the child secret into a non-persistent partition as a session cookie.
 *
 * ``fromPartition`` without a ``persist:`` prefix is memory-only, and no
 * ``expirationDate`` keeps the cookie a session cookie inside it: an
 * application restart destroys both, which is why a restart must re-run the
 * native authorization instead of resuming from disk.
 */
async function installChildCookie(partition: string, value: string): Promise<void> {
  const target = electronSession.fromPartition(partition)
  // Chromium treats localhost as a secure context, so Secure cookies work on
  // ``http://localhost``; for any other http origin (should not happen) drop
  // Secure so the partition can still hold the child session.
  const secure = !state.backendOrigin.startsWith('http://')
    || /^http:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?\/?$/i.test(state.backendOrigin)
  await target.cookies.set({
    url: state.backendOrigin,
    name: WEB_SESSION_COOKIE,
    value,
    path: '/',
    httpOnly: true,
    secure,
    sameSite: 'lax',
  })
}

/**
 * Destroy every container partition this process created.
 *
 * Called on logout, on a password change and when the backend origin changes:
 * the server has already revoked the pairing by then, and the local copy of the
 * child cookie must not outlive it. Best-effort by design -- a partition that
 * cannot be cleared must not keep the user signed in, and the server-side
 * revocation is what actually ends the authorization.
 */
export async function clearWebChildSessions(): Promise<void> {
  const partitions = Array.from(childPartitions)
  childPartitions.clear()
  for (const partition of partitions) {
    try {
      const target = electronSession.fromPartition(partition)
      await target.clearStorageData({ storages: ['cookies'] })
    } catch {
      /* the partition is already gone */
    }
  }
}

//: Monotonic context counter. It never resets -- not even across a re-login --
//: so a response that arrives after a switch can always be told apart from the
//: current context.
let epochCounter = 1

function nextEpoch(): number {
  return ++epochCounter
}

/** The authoritative self projection, read from ``/auth/me``. */
async function loadProjection(token: string, origin: string): Promise<BrokerSession> {
  const reply = await fetchWithToken('/auth/me', token, undefined, 'GET', origin)
  if (reply.status === 401 || reply.status === 403) {
    throw new BrokerError('unauthorized', 'the session is no longer valid', reply.status)
  }
  if (reply.status !== 200) {
    throw new BrokerError('projection_failed', 'the account projection could not be read', reply.status)
  }
  const data = JSON.parse(reply.body) as {
    user?: { id?: string; username?: string; display_name?: string; is_platform_admin?: boolean }
    must_change_password?: boolean
    tenants?: Array<{ id: string; code: string; name: string }>
  }
  return projectionFrom(data, null)
}

function projectionFrom(
  data: {
    user?: { id?: string; username?: string; display_name?: string; is_platform_admin?: boolean }
    must_change_password?: boolean
    tenants?: Array<{ id: string; code: string; name: string }>
  },
  previousTenant: string | null,
): BrokerSession {
  const user = data.user || {}
  const tenants = (data.tenants || []).map((t) => ({ id: t.id, code: t.code, name: t.name }))
  // The tenant comes only from this authoritative list; never from a username,
  // an Agent id or a remembered default. A single tenant is auto-selected so a
  // member is not asked to choose between one option; zero tenants stays null
  // (account/platform surfaces remain usable, tenant business does not).
  let tenantId: string | null = null
  if (tenants.length === 1) tenantId = tenants[0].id
  else if (previousTenant && tenants.some((t) => t.id === previousTenant)) tenantId = previousTenant
  return {
    userId: user.id || '',
    username: user.username || '',
    displayName: user.display_name || '',
    isPlatformAdmin: !!user.is_platform_admin,
    mustChangePassword: !!data.must_change_password,
    tenants,
    tenantId,
    epoch: state.session?.epoch || 1,
  }
}

/**
 * One credentialed request that is not a business call.
 *
 * ``method`` is explicit rather than defaulted at the call site: the account
 * surface mixes a read (``/auth/me``) with a write (``/auth/logout``), and a
 * bare ``fetch`` is a GET -- which the console answers with 405 for the sign-out,
 * leaving the server-side pairing alive after the user asked to leave.
 */
async function fetchWithToken(path: string, token: string, tenant?: string,
                              method: 'GET' | 'POST' = 'GET', origin = state.backendOrigin): Promise<RawReply> {
  const headers: Record<string, string> = { Authorization: `Bearer ${token}`, Accept: 'application/json' }
  if (tenant) headers['X-Tenant-ID'] = tenant
  const res = await fetch(`${origin}${path}`, { method, headers, redirect: 'error' })
  return {
    status: res.status,
    statusText: res.statusText,
    contentType: res.headers.get('content-type') || '',
    body: await res.text(),
    // This transport never consumes a Set-Cookie; it is read nowhere else.
    setCookies: [],
  }
}

// --------------------------------------------------------------------------- //
// Public operations
// --------------------------------------------------------------------------- //

/** Begin the browser authorization and resolve with the new projection. */
export async function beginAuthorization(): Promise<BrokerSession> {
  cancelPendingAuthorization()
  const generation = authorizationGeneration
  for (;;) {
    const result = await authorizeInteractive(generation)
    if (!('serverOrigin' in result)) return result
    // The old callback is closed and no code was exchanged. Select the server
    // through the same config owner as Settings, then mint fresh PKCE + state.
    // Dynamic import avoids a startup cycle (config-ipc also uses the broker).
    const { selectAuthorizationServer } = await import('./remote/config-ipc')
    if (generation !== authorizationGeneration) {
      throw new BrokerError('authorization_cancelled', 'authorization was cancelled')
    }
    if (state.session) {
      const ended = await logout()
      if (!ended.ok) throw new BrokerError('logout_incomplete', ended.message)
    }
    selectAuthorizationServer(result.serverOrigin)
  }
}

/** Unauthenticated ``/auth/check``: is a login required at all? */
export async function probeAuth(): Promise<{ authRequired: boolean; identityMode: string }> {
  if (!state.backendOrigin) throw new BrokerError('backend_unavailable', 'backend is not ready')
  const reply = await send('/auth/check', { method: 'GET', withTenant: false, withToken: false })
  if (reply.status !== 200) throw new BrokerError('probe_failed', 'the identity mode could not be read', reply.status)
  const data = JSON.parse(reply.body) as { auth_required?: boolean; authenticated?: boolean; identity_mode?: string }
  state.identityMode = data.identity_mode || ''
  state.authRequired = !!data.auth_required
  state.probeFailed = false
  return { authRequired: state.authRequired, identityMode: state.identityMode }
}

export function status(): BrokerStatus {
  return {
    blockedReason: state.blockedReason,
    session: state.session ? { ...state.session.projection } : null,
    authRequired: state.authRequired,
    identityMode: state.identityMode,
  }
}

/** Switch the business tenant. Aborts the old epoch and drops its assets. */
export function selectTenant(tenantId: string): BrokerSession {
  const session = state.session
  if (!session) throw new BrokerError('unauthorized', 'not signed in', 401)
  if (tenantId && !session.projection.tenants.some((t) => t.id === tenantId)) {
    throw new BrokerError('invalid_tenant', 'that tenant is not available to this account', 403)
  }
  if (session.projection.tenantId === tenantId) return { ...session.projection }
  // Bump first, then cancel: a response that lands after this point belongs to
  // the previous epoch and is discarded rather than rendered into the new one.
  const epoch = nextEpoch()
  abortInFlight()
  state.proxy?.dropEpochsExcept(epoch)
  session.epoch = epoch
  session.projection = { ...session.projection, tenantId: tenantId || null, epoch }
  return { ...session.projection }
}

/** Refresh the authoritative projection (after a password change, say). */
export async function refreshProjection(): Promise<BrokerSession> {
  const session = state.session
  if (!session) throw new BrokerError('unauthorized', 'not signed in', 401)
  const reply = await fetchWithToken('/auth/me', session.token)
  if (reply.status === 401 || reply.status === 403) {
    await clearSession()
    throw new BrokerError('unauthorized', 'the session is no longer valid', reply.status)
  }
  if (reply.status !== 200) {
    throw new BrokerError('projection_failed', 'the account projection could not be read', reply.status)
  }
  const data = JSON.parse(reply.body)
  session.projection = projectionFrom(data, session.projection.tenantId)
  session.projection.epoch = session.epoch
  return { ...session.projection }
}

async function clearSession(): Promise<void> {
  abortInFlight()
  // The server revoked the pairing before this runs (logout / password change),
  // so the local child cookie and its partition go too.
  await clearWebChildSessions()
  state.session = null
  state.blockedReason = ''
  state.proxy?.clear()
}

/**
 * Whether the server has already forgotten ``token``.
 *
 * ``/auth/me`` resolves the caller from the credential alone and is a read, so
 * its only credential answer is the truth about the session: 200 while it is
 * live, 401 once it is gone, 403 for a disabled account (unusable either way).
 * Written as a positive test rather than "not 200", so a server error is never
 * mistaken for proof that a session ended.
 */
async function sessionIsGone(token: string): Promise<boolean> {
  try {
    const reply = await fetchWithToken('/auth/me', token)
    return reply.status === 401 || reply.status === 403
  } catch {
    return false
  }
}

/**
 * Sign out: revoke the server session first, then clear local state.
 *
 * Revocation failing is reported as a failure. The UI must not claim the
 * session is gone while the server still honours it.
 *
 * A sign-out that is not *confirmed* is not automatically a failure, though.
 * In the paired state the container's own page-level logout has already revoked
 * the native parent -- that is what revoking a pair means -- so this second call
 * arrives holding a credential the identity store no longer knows. The route
 * gates every write through its CSRF rule, and a request whose only credential
 * is a Bearer that fails to authenticate is refused as `cross_origin` (403), a
 * *session* fact reported as an *origin* one. Status alone cannot tell that
 * apart from a live session the server declined to revoke, so the session itself
 * is asked (`/auth/me`): proof that it is gone means the sign-out the caller
 * asked for has already happened, and blocking on it would freeze the account
 * for a state the user is already in.
 */
export async function logout(): Promise<{ ok: boolean; revoked: boolean; message: string }> {
  const session = state.session
  if (!session) {
    await clearSession()
    return { ok: true, revoked: true, message: '' }
  }
  let revoked = false
  try {
    const reply = await fetchWithToken('/auth/logout', session.token, undefined, 'POST')
    revoked = reply.status === 200 || reply.status === 401
    if (!revoked) revoked = await sessionIsGone(session.token)
  } catch {
    revoked = false
  }
  if (!revoked) {
    // Keep the (still valid) session but freeze business traffic: continuing to
    // act on a session the server never revoked is the failure mode the design
    // forbids. The user can retry the sign-out.
    state.blockedReason = 'logout_incomplete'
    throw new BrokerError('logout_incomplete',
      'the server did not confirm the sign-out; requests are stopped until it succeeds')
  }
  await clearSession()
  return { ok: true, revoked: true, message: '' }
}

/** Change the account password through the broker (personal domain). */
export async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
  const session = state.session
  if (!session) throw new BrokerError('unauthorized', 'not signed in', 401)
  const reply = await send('/auth/password', {
    method: 'POST',
    contentType: 'application/json',
    withTenant: false,
    withToken: true,
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  })
  if (reply.status !== 200) {
    let message = 'the password could not be changed'
    let code = 'password_change_failed'
    try {
      const parsed = JSON.parse(reply.body) as { message?: string; code?: string }
      if (parsed.message) message = parsed.message
      if (parsed.code) code = parsed.code
    } catch {
      /* keep the generic message */
    }
    throw new BrokerError(code, message, reply.status)
  }
  // The server revokes the session on a successful change, so the native
  // session is gone too and the account must authorize again.
  await clearSession()
}

/** The business transport used by every renderer request. */
export async function requestBusiness(req: BusinessRequest): Promise<RawReply> {
  if (state.blockedReason) {
    throw new BrokerError(state.blockedReason, 'requests are stopped until the sign-out completes', 503)
  }
  const path = req.path || ''
  if (!path.startsWith('/') || path.startsWith('//') || /^[a-z]+:/i.test(path)) {
    throw new BrokerError('invalid_path', 'only same-backend paths are allowed', 400)
  }
  if (isBrokerOwnedPath(path)) {
    // The credential-bearing endpoints are not reachable from the renderer, not
    // even with a valid session: they have dedicated IPC that owns the secrets.
    throw new BrokerError('forbidden_path', 'that endpoint is not available to the renderer', 403)
  }
  const session = state.session
  if (!session) throw new BrokerError('unauthorized', 'not signed in', 401)
  const epoch = session.epoch
  const controller = new AbortController()
  state.inFlight.add(controller)
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  try {
    const reply = await send(path, {
      method: (req.method || 'GET').toUpperCase(),
      contentType: req.form ? undefined : 'application/json',
      body: req.body,
      withTenant: true,
      withToken: true,
      signal: controller.signal,
    })
    if (state.session?.epoch !== epoch) {
      // The tenant/account changed while this was in flight. Never render it,
      // and never re-send a write under the new context.
      throw new BrokerError('stale_context', 'the context changed while this request was in flight', 409)
    }
    if (reply.status === 401) {
      // The server has rejected the native session (revoked, expired or the
      // account was disabled). Drop it here so the UI immediately returns to
      // the sign-in flow instead of retrying a dead credential.
      await clearSession()
      throw new BrokerError('unauthorized', 'the session is no longer valid', 401)
    }
    return reply
  } catch (e) {
    if (e instanceof BrokerError) throw e
    if (state.session && state.session.epoch !== epoch) {
      throw new BrokerError('stale_context', 'the context changed while this request was in flight', 409)
    }
    throw new BrokerError('network_error', networkMessage(e))
  } finally {
    clearTimeout(timer)
    state.inFlight.delete(controller)
  }
}

/** Multipart upload: the renderer sends entries, the broker builds the body. */
export async function requestUpload(req: {
  path: string
  method?: string
  form: BrokerForm
}): Promise<RawReply> {
  const boundary = `----cowagent${randomBytes(12).toString('hex')}`
  const chunks: Buffer[] = []
  for (const field of req.form.fields) {
    chunks.push(Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="${field.name}"\r\n\r\n${field.value}\r\n`,
      'utf8',
    ))
  }
  for (const file of req.form.files) {
    chunks.push(Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="${file.name}"; filename="${file.filename}"\r\n` +
      `Content-Type: ${file.contentType}\r\n\r\n`,
      'utf8',
    ))
    chunks.push(Buffer.from(file.bytes))
    chunks.push(Buffer.from('\r\n', 'utf8'))
  }
  chunks.push(Buffer.from(`--${boundary}--\r\n`, 'utf8'))
  const path = req.path || ''
  if (!path.startsWith('/') || isBrokerOwnedPath(path)) {
    throw new BrokerError('invalid_path', 'only same-backend paths are allowed', 400)
  }
  const session = state.session
  if (!session) throw new BrokerError('unauthorized', 'not signed in', 401)
  if (state.blockedReason) {
    throw new BrokerError(state.blockedReason, 'requests are stopped until the sign-out completes', 503)
  }
  const epoch = session.epoch
  const reply = await send(path, {
    method: (req.method || 'POST').toUpperCase(),
    contentType: `multipart/form-data; boundary=${boundary}`,
    // A genuine Buffer, not a binary *string*: fetch would re-encode a string
    // as UTF-8 and corrupt every uploaded byte above 0x7f.
    body: Buffer.concat(chunks),
    withTenant: true,
    withToken: true,
  })
  if (state.session?.epoch !== epoch) {
    throw new BrokerError('stale_context', 'the context changed while this upload was in flight', 409)
  }
  return reply
}

/**
 * Mint a short-lived loopback URL for a transport that cannot send headers.
 *
 * The renderer gets ``http://127.0.0.1:<port>/a/<opaque>`` -- no token, no
 * tenant, no backend origin. The proxy attaches the current credential when the
 * request arrives, so a stream that reconnects after a tenant switch uses the
 * new context, and an expired id is refused.
 */
export async function mintAssetUrl(path: string, kind: 'get' | 'stream'): Promise<string> {
  const session = state.session
  if (!session) throw new BrokerError('unauthorized', 'not signed in', 401)
  if (!path.startsWith('/') || isBrokerOwnedPath(path)) {
    throw new BrokerError('invalid_path', 'only same-backend paths are allowed', 400)
  }
  setupAssetProxy()
  await state.proxy!.start()
  return state.proxy!.mint(path, kind, session.epoch)
}

/** The credentialed fetch the asset proxy performs on the renderer's behalf. */
async function fetchAssetUpstream(target: AssetTarget): Promise<UpstreamReply> {
  const session = state.session
  if (!session || session.epoch !== target.epoch || !state.backendOrigin) {
    throw new Error('asset context is no longer current')
  }
  const headers: Record<string, string> = { Authorization: `Bearer ${session.token}` }
  if (session.projection.tenantId && wantsTenantHeader(target.path)) {
    headers['X-Tenant-ID'] = session.projection.tenantId
  }
  /**
   * Use the raw request rather than fetch(): a stream must reach the caller
   * unbuffered, and the proxy needs to pipe it. ``net``/``http`` here never
   * leaves the machine.
   */
  const controller = new AbortController()
  const url = new URL(`${state.backendOrigin}${target.path}`)
  const request = http.request({
    host: url.hostname,
    port: url.port,
    path: `${url.pathname}${url.search}`,
    method: 'GET',
    headers,
  })
  const onAbort = () => request.destroy()
  controller.signal.addEventListener('abort', onAbort)
  const reply = await new Promise<UpstreamReply>((resolve, reject) => {
    request.once('response', (response) => {
      resolve({
        status: response.statusCode || 502,
        headers: response.headers,
        stream: response,
        cancel: () => {
          controller.abort()
        },
      })
    })
    request.once('error', reject)
    request.end()
  })
  return reply
}

// --------------------------------------------------------------------------- //
// IPC surface (narrow) + sender validation
// --------------------------------------------------------------------------- //

interface TrustedWindowSource {
  isTrustedSender: (event: IpcMainInvokeEvent) => boolean
}

let trusted: TrustedWindowSource = { isTrustedSender: () => false }

/** Install the window/entry check used by every channel below. */
export function setTrustedSenderCheck(check: (event: IpcMainInvokeEvent) => boolean): void {
  trusted = { isTrustedSender: check }
}

function guard(event: IpcMainInvokeEvent): void {
  if (!trusted.isTrustedSender(event)) {
    throw new Error('untrusted sender')
  }
}

function failure(e: unknown): { ok: false; code: string; message: string; status: number } {
  if (e instanceof BrokerError) {
    return { ok: false, code: e.code, message: e.message, status: e.status }
  }
  // Never echo an arbitrary error: it could carry a URL or a header.
  return { ok: false, code: 'unexpected_error', message: 'the request could not be completed', status: 0 }
}

/**
 * Register the narrowed IPC channels.
 *
 * The renderer gets: probe, status, begin, cancel, logout, tenant select,
 * password change, one business request channel, one upload channel, and asset
 * minting. It cannot name an origin, set a header, read a token, or reach the
 * native auth endpoints -- there is no channel that would carry one.
 */
export function setupAuthBrokerIPC(): void {
  ipcMain.handle('desktop-auth-probe', async (event) => {
    guard(event)
    try {
      return { ok: true, ...(await probeAuth()) }
    } catch (e) {
      // A probe failure is reported as "unknown", never as "not required": a
      // down identity store must not open the app.
      return { ...failure(e), authRequired: true, identityMode: '' }
    }
  })

  ipcMain.handle('desktop-auth-status', (event) => {
    guard(event)
    return { ok: true, ...status() }
  })

  ipcMain.handle('desktop-auth-begin', async (event) => {
    guard(event)
    try {
      const session = await beginAuthorization()
      // Spec: the in-process backend may be bound as the Web workbench without
      // a stored HTTPS profile. Do it after native sign-in so the parent session
      // exists for the child Cookie bootstrap.
      try {
        const { tryAutoBindLocalWeb } = await import('./remote/local-web-bind')
        // Consent may have selected a remote profile while the browser was up.
        void tryAutoBindLocalWeb(true)
      } catch {
        /* optional path; local React shell remains usable */
      }
      return { ok: true, session }
    } catch (e) {
      return failure(e)
    }
  })

  ipcMain.handle('desktop-auth-cancel', (event) => {
    guard(event)
    cancelPendingAuthorization()
    return { ok: true }
  })

  ipcMain.handle('desktop-auth-logout', async (event) => {
    guard(event)
    try {
      // Prefer tearing down an attached container (which also revokes); a bare
      // logout while the Web view is up would leave a live Cookie partition.
      try {
        const { isRemoteContainerAttached, detachRemoteContainer } =
          await import('./remote/remote-container-ipc')
        if (isRemoteContainerAttached()) {
          return { ...(await detachRemoteContainer()) }
        }
      } catch {
        /* fall through to native logout */
      }
      return { ...(await logout()) }
    } catch (e) {
      return failure(e)
    }
  })

  ipcMain.handle('desktop-tenant-select', (event, tenantId: string) => {
    guard(event)
    try {
      return { ok: true, session: selectTenant(typeof tenantId === 'string' ? tenantId : '') }
    } catch (e) {
      return failure(e)
    }
  })

  ipcMain.handle('desktop-auth-refresh', async (event) => {
    guard(event)
    try {
      return { ok: true, session: await refreshProjection() }
    } catch (e) {
      return failure(e)
    }
  })

  ipcMain.handle('desktop-password-change', async (event, payload: { oldPassword?: string; newPassword?: string }) => {
    guard(event)
    try {
      await changePassword(String(payload?.oldPassword || ''), String(payload?.newPassword || ''))
      return { ok: true }
    } catch (e) {
      return failure(e)
    }
  })

  ipcMain.handle('desktop-request', async (event, req: BusinessRequest) => {
    guard(event)
    try {
      const reply = await requestBusiness({
        path: String(req?.path || ''),
        method: String(req?.method || 'GET'),
        body: req?.body,
      })
      return { ok: true, ...reply }
    } catch (e) {
      return failure(e)
    }
  })

  ipcMain.handle('desktop-upload', async (event, req: { path: string; method?: string; form: BrokerForm }) => {
    guard(event)
    try {
      const reply = await requestUpload({
        path: String(req?.path || ''),
        method: String(req?.method || 'POST'),
        form: req?.form || { fields: [], files: [] },
      })
      return { ok: true, ...reply }
    } catch (e) {
      return failure(e)
    }
  })

  ipcMain.handle('desktop-asset-url', async (event, req: { path: string; kind?: 'get' | 'stream' }) => {
    guard(event)
    try {
      const url = await mintAssetUrl(String(req?.path || ''), req?.kind === 'stream' ? 'stream' : 'get')
      return { ok: true, url }
    } catch (e) {
      return failure(e)
    }
  })

  // The synchronous twin of the channel above, for the URL helpers in
  // api/client.ts that feed JSX `src=` attributes and ``window.open`` -- call
  // sites whose signatures must stay synchronous. Minting is a map insert in
  // this process, so the block is bounded; and, exactly like the async channel,
  // it can only ever return an opaque loopback URL.
  ipcMain.on('desktop-asset-url-sync', (event, req: { path: string; kind?: 'get' | 'stream' }) => {
    try {
      guard(event)
      const session = state.session
      if (!session) throw new BrokerError('unauthorized', 'not signed in', 401)
      const path = String(req?.path || '')
      if (!path.startsWith('/') || isBrokerOwnedPath(path)) {
        throw new BrokerError('invalid_path', 'only same-backend paths are allowed', 400)
      }
      setupAssetProxy()
      const proxy = state.proxy!
      // The listener is started on first use; a sync channel cannot await it, so
      // the port is bound ahead of time by setupAssetProxy()/start() below.
      if (!proxy.isRunning()) {
        void proxy.start()
        throw new BrokerError('asset_proxy_starting', 'the asset proxy is starting', 503)
      }
      event.returnValue = { ok: true, url: proxy.mint(path, req?.kind === 'stream' ? 'stream' : 'get', session.epoch) }
    } catch (e) {
      event.returnValue = failure(e)
    }
  })
}

/**
 * The renderer document this build ships, resolved next to the compiled main
 * process. It is the only ``file:`` document the window is ever allowed to be
 * on, so it is compared exactly rather than by shape.
 */
const RENDERER_ENTRY = path.join(__dirname, '..', 'renderer', 'index.html')

/** Exported for the registration check in the main process entry point. */
export function isRegisteredEntryUrl(url: string): boolean {
  if (!url) return false
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return false
  }
  // Only the *document* decides this, never the fragment. The shell is a
  // HashRouter, so every page the user opens lives in the fragment of the same
  // index.html -- comparing the whole URL meant the window stopped being a
  // trusted sender the moment any route was entered, and every guarded channel
  // (the connection shell's included) answered "untrusted sender". A query
  // string is ignored for the same reason: it cannot change which file loaded.
  if (parsed.protocol === 'file:') {
    // Exactly the bundled document: a dropped file, another app's document that
    // happens to sit at the same relative path, or an injected page is not a
    // registered entry. fileURLToPath decodes percent-escapes so an install
    // path containing spaces still compares equal.
    let entry: string
    try {
      entry = fileURLToPath(parsed)
    } catch {
      return false
    }
    return entry === RENDERER_ENTRY
  }
  if (app.isPackaged) return false
  // Development accepts only the project's own Vite dev server (index.html at
  // its root) or the built document.
  return /^http:\/\/localhost:5173\//.test(`${parsed.origin}${parsed.pathname}`)
}

/** The MainWindow the broker will accept calls from. */
export function isTrustedWindowFrame(event: IpcMainInvokeEvent, window: BrowserWindow | null): boolean {
  if (!window || window.isDestroyed()) return false
  if (event.sender !== window.webContents) return false
  // Only the top frame: a nested iframe, a webview or a popup is refused.
  if (event.senderFrame && event.senderFrame !== event.sender.mainFrame) return false
  const url = event.senderFrame?.url || ''
  return isRegisteredEntryUrl(url)
}
