// Desktop remote connection: state machine + phase-1 protocol negotiation.
//
// Change ``add-desktop-remote-web-workbench`` (task 2.6). Before the app can
// load a remote workbench it has to answer, honestly, three separate questions
// that used to be one generic "could not connect":
//
//   * is the server reachable at all, and did TLS actually validate?
//   * does it speak a protocol major this client understands?
//   * is the *identity* step what failed (so retrying is pointless)?
//
// Keeping those apart is the point of this module: a TLS failure must never be
// retried as if it were a flaky network, a login failure must stop the retry
// loop rather than hammer the server, and a certificate error must surface as a
// refusal -- never as a silent downgrade to HTTP or a skipped verification.
//
// Pure Node (no Electron import), with the fetch implementation injected, so
// ``tests/test_desktop_connection.cjs`` drives the real transitions.

import { parseServerOrigin } from './profiles'

/** The protocol majors this client speaks, and which are required to operate. */
export const CLIENT_PROTOCOLS = {
  /** Required: the native->Web session bootstrap. */
  web_session: { major: 1, minor: 0, required: true },
  /** Required: the narrow ``window.desktopHost`` bridge. */
  bridge: { major: 1, minor: 0, required: true },
  /** Optional: local-file access. An unknown major disables the feature only. */
  files: { major: 1, minor: 0, required: false },
} as const

export type ProtocolName = keyof typeof CLIENT_PROTOCOLS

export interface ProtocolVersion {
  major: number
  minor: number
}

export interface RemoteMeta {
  remote_web: { implemented: boolean; accepted: boolean; configured: boolean; available: boolean; reason: string }
  protocols: Record<string, ProtocolVersion>
  entry_path: string
  console_entry_paths: string[]
  features: Record<string, { available: boolean; reason: string }>
}

/** The connection states from design D11, plus the failure states. */
export type ConnectionState =
  | 'unconfigured'
  | 'probing'
  | 'authorizing'
  | 'bootstrapping'
  | 'ready'
  | 'reconnecting'
  | 'auth_required'
  | 'incompatible'
  | 'blocked'

/** Why a step failed, in the categories the UI and the retry policy need. */
export type FailureKind =
  | 'tls'
  | 'network'
  | 'http'
  | 'protocol'
  | 'identity'
  | 'downgrade'

export interface ConnectionFailure {
  kind: FailureKind
  /** Stable code (an error/diagnostic key, never a URL or a secret). */
  code: string
  message: string
}

export type ProbeResult =
  | { ok: true; meta: RemoteMeta }
  | { ok: false; failure: ConnectionFailure }

/** TLS error codes that mean "the certificate chain was not trusted". */
const TLS_CODES = new Set([
  'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
  'SELF_SIGNED_CERT_IN_CHAIN',
  'DEPTH_ZERO_SELF_SIGNED_CERT',
  'CERT_HAS_EXPIRED',
  'ERR_TLS_CERT_ALTNAME_INVALID',
  'UNABLE_TO_GET_ISSUER_CERT_LOCALLY',
  'CERT_UNTRUSTED',
  'ERR_TLS_CERT_ALTNAME_INVALID',
])

/**
 * Classify a thrown fetch error without ever echoing the URL.
 *
 * Node wraps the underlying socket error in ``cause``; the TLS codes there are
 * the only reliable signal that the certificate -- not the network -- is the
 * problem. Anything unrecognized is a plain network failure.
 */
export function classifyFetchError(err: unknown): ConnectionFailure {
  const cause = (err as { cause?: { code?: string } } | undefined)?.cause
  const code = cause?.code || (err as { code?: string } | undefined)?.code || ''
  const name = (err as { name?: string } | undefined)?.name || ''
  if (TLS_CODES.has(code) || /CERT|TLS|SSL/i.test(code) || /CERT|TLS|SSL/i.test(name)) {
    return { kind: 'tls', code: 'tls_verification_failed', message: 'the server certificate could not be verified' }
  }
  return { kind: 'network', code: 'network_unreachable', message: 'the server could not be reached' }
}

/**
 * Fetch and parse ``GET /api/desktop/meta`` from an exact origin.
 *
 * Public remote servers must be HTTPS (``parseServerOrigin``). The one
 * exception is the in-process local backend: callers pass that exact origin as
 * ``allowHttpOrigin`` so a registered ``http://127.0.0.1:<port>`` /
 * ``http://localhost:<port>`` can be probed without inventing a stored HTTP
 * profile. ``redirect: 'error'`` is deliberate: an authenticated redirect must
 * never be followed (the same rule the broker enforces).
 */
export async function probeServer(
  rawOrigin: unknown,
  fetchImpl: typeof fetch,
  options?: { allowHttpOrigin?: string },
): Promise<ProbeResult> {
  let origin = ''
  const allowed = typeof options?.allowHttpOrigin === 'string'
    ? options.allowHttpOrigin.replace(/\/+$/, '')
    : ''
  const candidate = typeof rawOrigin === 'string' ? rawOrigin.trim().replace(/\/+$/, '') : ''
  if (allowed && candidate === allowed && isRegisteredLocalHttpOrigin(candidate)) {
    origin = candidate
  } else {
    const parsed = parseServerOrigin(rawOrigin)
    if (!parsed.ok) {
      const kind: FailureKind = parsed.reason === 'scheme_not_https' ? 'downgrade' : 'http'
      return { ok: false, failure: { kind, code: parsed.reason, message: 'that address is not a supported server' } }
    }
    origin = parsed.origin
  }
  let response: Response
  try {
    response = await fetchImpl(`${origin}/api/desktop/meta`, {
      method: 'GET',
      redirect: 'error',
      headers: { Accept: 'application/json', 'Cache-Control': 'no-store' },
    })
  } catch (err) {
    return { ok: false, failure: classifyFetchError(err) }
  }
  if (response.status !== 200) {
    // 401/403 here would still be a *protocol* answer, not an identity failure:
    // the meta endpoint is public, so any refusal is the server's shape.
    return {
      ok: false,
      failure: { kind: 'http', code: `http_${response.status}`, message: 'the server did not offer desktop metadata' },
    }
  }
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    return { ok: false, failure: { kind: 'protocol', code: 'invalid_metadata', message: 'the server returned unreadable metadata' } }
  }
  const meta = parseMeta(payload)
  if (!meta) {
    return { ok: false, failure: { kind: 'protocol', code: 'invalid_metadata', message: 'the server returned incomplete metadata' } }
  }
  return { ok: true, meta }
}

/** Loopback HTTP origins that may stand in for the in-process backend. */
function isRegisteredLocalHttpOrigin(origin: string): boolean {
  try {
    const url = new URL(origin)
    if (url.protocol !== 'http:') return false
    if (url.username || url.password || url.search || url.hash) return false
    if (url.pathname && url.pathname !== '/') return false
    const host = (url.hostname || '').toLowerCase()
    return host === '127.0.0.1' || host === 'localhost' || host === '::1'
  } catch {
    return false
  }
}

/** Validate the metadata envelope; null when a required field is missing. */
export function parseMeta(payload: unknown): RemoteMeta | null {
  if (!payload || typeof payload !== 'object') return null
  const data = (payload as { data?: unknown }).data
  if (!data || typeof data !== 'object') return null
  const d = data as Record<string, unknown>
  if (!d.protocols || typeof d.protocols !== 'object') return null
  if (typeof d.entry_path !== 'string') return null
  if (!Array.isArray(d.console_entry_paths) || !d.console_entry_paths.every((p) => typeof p === 'string')) return null
  const remote = d.remote_web as RemoteMeta['remote_web'] | undefined
  if (!remote || typeof remote !== 'object') return null
  const versions: Record<string, ProtocolVersion> = {}
  for (const [name, value] of Object.entries(d.protocols as Record<string, unknown>)) {
    const v = value as { major?: unknown; minor?: unknown } | undefined
    if (v && typeof v.major === 'number' && typeof v.minor === 'number') {
      versions[name] = { major: v.major, minor: v.minor }
    }
  }
  const features = (d.features && typeof d.features === 'object' ? d.features : {}) as RemoteMeta['features']
  return {
    remote_web: remote,
    protocols: versions,
    entry_path: d.entry_path,
    console_entry_paths: d.console_entry_paths as string[],
    features,
  }
}

export interface NegotiationResult {
  ok: boolean
  /** Optional capabilities to disable rather than refuse the connection. */
  disabled: ProtocolName[]
  failure?: ConnectionFailure
}

/**
 * Compare the server's protocol majors against this client's.
 *
 * A *required* protocol whose major differs is a hard incompatibility -- the
 * handshake would be misinterpreted, so the client must refuse rather than
 * guess. An *optional* protocol with an unknown major disables just that
 * feature: the workbench still loads, without local files.
 */
export function negotiate(meta: RemoteMeta): NegotiationResult {
  const disabled: ProtocolName[] = []
  for (const name of Object.keys(CLIENT_PROTOCOLS) as ProtocolName[]) {
    const client = CLIENT_PROTOCOLS[name]
    const server = meta.protocols[name]
    const compatible = !!server && server.major === client.major
    if (compatible) continue
    if (client.required) {
      return {
        ok: false,
        disabled: [],
        failure: {
          kind: 'protocol',
          code: server ? `protocol_major_${name}` : `protocol_missing_${name}`,
          message: 'the server does not speak a compatible protocol',
        },
      }
    }
    disabled.push(name)
  }
  return { ok: true, disabled }
}

/** Whether a failure should be retried automatically (design D11). */
export function isRetryable(kind: FailureKind): boolean {
  // TLS and protocol need a configuration/client fix, not a retry; identity
  // needs the user to sign in again. Only transient transport failures retry.
  return kind === 'network'
}

/** The reconnect schedule: 1,2,4,8,16,30s capped, with +/-20% jitter. */
export function retryDelay(attempt: number, rand: () => number = Math.random): number {
  const steps = [1, 2, 4, 8, 16, 30]
  const base = steps[Math.min(Math.max(attempt, 0), steps.length - 1)] * 1000
  const jitter = (rand() * 2 - 1) * 0.2
  return Math.round(base * (1 + jitter))
}

/** The events the machine reacts to. */
export type ConnectionEvent =
  | { type: 'configure' }
  | { type: 'probe_ok' }
  | { type: 'probe_failed'; failure: ConnectionFailure }
  | { type: 'authorize' }
  | { type: 'authorized' }
  | { type: 'authorize_failed'; failure: ConnectionFailure }
  | { type: 'bootstrap' }
  | { type: 'ready' }
  | { type: 'lost' }
  | { type: 'blocked'; failure: ConnectionFailure }
  | { type: 'reset' }

/**
 * A minimal, deterministic connection state machine.
 *
 * ``blocked`` is terminal for business traffic and is only left by ``reset``
 * (the user's explicit retry), which is what keeps a failed offline revocation
 * from quietly resuming as if the session were still valid.
 */
export class ConnectionMachine {
  private current: ConnectionState = 'unconfigured'
  private failure: ConnectionFailure | null = null
  private attempt = 0

  get state(): ConnectionState {
    return this.current
  }

  get lastFailure(): ConnectionFailure | null {
    return this.failure
  }

  get retryAttempt(): number {
    return this.attempt
  }

  dispatch(event: ConnectionEvent): ConnectionState {
    switch (event.type) {
      case 'reset':
        this.failure = null
        this.attempt = 0
        this.current = 'unconfigured'
        break
      case 'configure':
        if (this.current === 'unconfigured' || this.current === 'reconnecting' || this.current === 'ready') {
          this.current = 'probing'
          this.failure = null
        }
        break
      case 'probe_ok':
        this.current = 'authorizing'
        this.failure = null
        break
      case 'probe_failed':
        this.failure = event.failure
        this.current = event.failure.kind === 'protocol' || event.failure.kind === 'downgrade'
          ? 'incompatible'
          : 'unconfigured'
        break
      case 'authorize_failed':
        this.failure = event.failure
        // Identity is not transient: do not schedule a retry.
        this.current = event.failure.kind === 'protocol' ? 'incompatible' : 'auth_required'
        break
      case 'authorize':
        this.current = 'authorizing'
        break
      case 'authorized':
        this.current = 'bootstrapping'
        this.failure = null
        break
      case 'bootstrap':
        this.current = 'bootstrapping'
        break
      case 'ready':
        this.current = 'ready'
        this.failure = null
        this.attempt = 0
        break
      case 'lost':
        // A transient loss is the only thing that enters the retry loop.
        this.attempt += 1
        this.current = isRetryable('network') ? 'reconnecting' : this.current
        break
      case 'blocked':
        this.failure = event.failure
        this.current = 'blocked'
        break
    }
    return this.current
  }
}
