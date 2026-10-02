// Web child session: the contract-checked half of task 3.7.
//
// The native parent mints an *independent* Web child session so the container
// never holds the native Bearer (design D8 / spec ``desktop-tenant-context``).
// The security of that step rests on refusing a reply that is not shaped
// exactly as the contract says -- a redirect, a token in the body, a cookie
// that is readable by page script or shared across hosts. Those refusals are
// the interesting part, and they are pure data checks, so they live here (no
// Electron import) and ``tests/test_desktop_web_session_bridge.cjs`` drives
// them for real.
//
// ``auth-broker.ts`` keeps only the part that needs Electron: installing the
// accepted cookie into an in-memory partition.

import { CLIENT_PROTOCOLS } from './connection'

/** Contract major this client speaks for the native->Web bootstrap. */
export const WEB_SESSION_MAJOR = CLIENT_PROTOCOLS.web_session.major

/** The cookie name the contract fixes (contracts/desktop/v1.json §2). */
export const WEB_SESSION_COOKIE = 'cow_session'

/** A session secret is a single base64url token; anything else is a refusal. */
const SECRET_PATTERN = /^[A-Za-z0-9_-]{20,512}$/

/** The same instance-id shape the server enforces, so we fail before the trip. */
const INSTANCE_ID_PATTERN = /^[A-Za-z0-9_-]{22,128}$/

export interface ParsedCookie {
  name: string
  value: string
  httpOnly: boolean
  secure: boolean
  sameSite: string
  path: string
  domain: string
  maxAge: number | null
}

export interface ChildCookie {
  name: string
  value: string
  /** Seconds the server said the child may live; 0 when it said nothing. */
  maxAge: number
}

export type CookieCheck =
  | { ok: true; cookie: ChildCookie }
  | { ok: false; code: string; message: string }

/** Parse one ``Set-Cookie`` header. Attribute names are case-insensitive. */
export function parseSetCookie(header: string): ParsedCookie | null {
  if (typeof header !== 'string' || !header) return null
  const parts = header.split(';')
  const pair = parts.shift() || ''
  const eq = pair.indexOf('=')
  if (eq <= 0) return null
  const parsed: ParsedCookie = {
    name: pair.slice(0, eq).trim(),
    value: pair.slice(eq + 1).trim(),
    httpOnly: false,
    secure: false,
    sameSite: '',
    path: '',
    domain: '',
    maxAge: null,
  }
  for (const raw of parts) {
    const attribute = raw.trim()
    if (!attribute) continue
    const split = attribute.indexOf('=')
    const name = (split === -1 ? attribute : attribute.slice(0, split)).trim().toLowerCase()
    const value = split === -1 ? '' : attribute.slice(split + 1).trim()
    if (name === 'httponly') parsed.httpOnly = true
    else if (name === 'secure') parsed.secure = true
    else if (name === 'samesite') parsed.sameSite = value
    else if (name === 'path') parsed.path = value
    else if (name === 'domain') parsed.domain = value
    else if (name === 'max-age') parsed.maxAge = Number.parseInt(value, 10)
  }
  return parsed
}

/** ``Set-Cookie`` values off a reply, whatever the runtime exposes. */
export function replySetCookies(headers: {
  get?: (name: string) => string | null
  getSetCookie?: () => string[]
} | null): string[] {
  if (!headers) return []
  if (typeof headers.getSetCookie === 'function') return headers.getSetCookie()
  const single = typeof headers.get === 'function' ? headers.get('set-cookie') : null
  return single ? [single] : []
}

/**
 * The one acceptable child cookie, or why there is none.
 *
 * Every requirement here is a way the delivery could go wrong: a cookie the
 * page could read (missing ``HttpOnly``), one that could travel over plain
 * HTTP (missing ``Secure``), one shared with sibling hosts (a ``Domain``), one
 * that escapes the container's path or CSRF model (wrong ``Path``/``SameSite``),
 * or -- worst -- the native Bearer itself being handed back as the "child".
 */
export function selectChildCookie(setCookies: string[], nativeToken: string): CookieCheck {
  const candidates = (setCookies || [])
    .map(parseSetCookie)
    .filter((cookie): cookie is ParsedCookie => cookie !== null && cookie.name === WEB_SESSION_COOKIE)
  if (candidates.length === 0) {
    return { ok: false, code: 'bootstrap_failed', message: 'the server did not deliver a child session cookie' }
  }
  if (candidates.length > 1) {
    return { ok: false, code: 'bootstrap_failed', message: 'the server delivered more than one child session cookie' }
  }
  const cookie = candidates[0]
  const refuse = (message: string): CookieCheck => ({ ok: false, code: 'bootstrap_failed', message })
  if (!SECRET_PATTERN.test(cookie.value)) return refuse('the child session cookie is not a session token')
  if (nativeToken && cookie.value === nativeToken) {
    return refuse('the child session cookie repeats the native session credential')
  }
  if (!cookie.httpOnly) return refuse('the child session cookie is readable by page script')
  if (!cookie.secure) return refuse('the child session cookie could travel over plain HTTP')
  if (cookie.domain) return refuse('the child session cookie is not host-only')
  if (cookie.path !== '/') return refuse('the child session cookie does not cover the application path')
  if (cookie.sameSite.toLowerCase() !== 'lax') {
    return refuse('the child session cookie does not declare SameSite=Lax')
  }
  return {
    ok: true,
    cookie: {
      name: cookie.name,
      value: cookie.value,
      maxAge: Number.isFinite(cookie.maxAge) && (cookie.maxAge as number) > 0 ? (cookie.maxAge as number) : 0,
    },
  }
}

export interface BootstrapReply {
  status: number
  contentType?: string
  body: string
  setCookies: string[]
}

export interface BootstrapAccepted {
  ok: true
  cookie: ChildCookie
  linkId: string
  expiresAt: number
  webProtocol: number
}

export type BootstrapCheck =
  | BootstrapAccepted
  | { ok: false; code: string; message: string; status: number }

function errorFrom(status: number, parsed: { code?: string; message?: string }): BootstrapCheck {
  return {
    ok: false,
    code: typeof parsed.code === 'string' && parsed.code ? parsed.code : 'bootstrap_failed',
    message: typeof parsed.message === 'string' && parsed.message
      ? parsed.message
      : 'the Web session could not be bootstrapped',
    status: status >= 400 ? status : 502,
  }
}

/**
 * Accept a bootstrap answer, or refuse it.
 *
 * A 2xx is not enough: the body must carry the link metadata and *must not*
 * carry the secret (the cookie is the only channel), and the cookie itself must
 * pass :func:`selectChildCookie`. A redirect can never reach here -- the
 * broker's transport refuses redirects outright -- but an HTML page or a
 * ``Location`` echo in the body is refused too, because a child secret that
 * leaks into the body is the failure this whole step exists to prevent.
 */
export function checkBootstrapReply(reply: BootstrapReply, nativeToken: string): BootstrapCheck {
  let parsed: {
    status?: string
    code?: string
    message?: string
    data?: { link_id?: unknown; expires_at?: unknown; web_protocol?: unknown; web_token?: unknown }
  } = {}
  try {
    parsed = JSON.parse(reply.body || '{}')
  } catch {
    return { ok: false, code: 'bootstrap_failed', message: 'the bootstrap answer was not JSON', status: reply.status >= 400 ? reply.status : 502 }
  }
  if (reply.status !== 200) return errorFrom(reply.status, parsed)
  const data = parsed.data
  if (!data || typeof data !== 'object') {
    return { ok: false, code: 'bootstrap_failed', message: 'the bootstrap answer carried no link data', status: 502 }
  }
  if (data.web_token !== undefined) {
    return { ok: false, code: 'bootstrap_failed', message: 'the bootstrap answer carried the session secret in its body', status: 502 }
  }
  const linkId = typeof data.link_id === 'string' ? data.link_id : ''
  if (!linkId) {
    return { ok: false, code: 'bootstrap_failed', message: 'the bootstrap answer carried no link id', status: 502 }
  }
  const expiresAt = Number(data.expires_at)
  if (!Number.isFinite(expiresAt) || expiresAt <= 0) {
    return { ok: false, code: 'bootstrap_failed', message: 'the bootstrap answer carried no expiry', status: 502 }
  }
  const webProtocol = Number(data.web_protocol)
  if (webProtocol !== WEB_SESSION_MAJOR) {
    return {
      ok: false, code: 'protocol_mismatch',
      message: 'the server answered with another Web session protocol major', status: 400,
    }
  }
  const selected = selectChildCookie(reply.setCookies || [], nativeToken)
  if (!selected.ok) return { ok: false, code: selected.code, message: selected.message, status: 502 }
  // Belt and braces: the accepted secret must not appear in the body either.
  if (reply.body.includes(selected.cookie.value)) {
    return { ok: false, code: 'bootstrap_failed', message: 'the bootstrap answer echoed the session secret', status: 502 }
  }
  return { ok: true, cookie: selected.cookie, linkId, expiresAt, webProtocol }
}

/** Validate a client-generated instance id before it becomes a partition name. */
export function isValidInstanceId(instanceId: string): boolean {
  return INSTANCE_ID_PATTERN.test(instanceId || '')
}

/**
 * The in-memory Electron partition for one container instance.
 *
 * No ``persist:`` prefix: the partition (and therefore the child cookie) only
 * ever exists in memory, so an application restart cannot resurrect it -- the
 * native authorization has to run again.
 */
export function partitionName(instanceId: string): string {
  return `desktop-remote-${instanceId}`
}
