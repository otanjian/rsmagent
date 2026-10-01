/**
 * The HTTP half of the explicit upload (task 9.7).
 *
 * Three calls, no more: reserve, send a window, commit. They are exactly the
 * endpoints the web workbench already uses for transfers, so a file the user
 * chooses to hand over travels the same path -- same quota reservation, same
 * ownership checks, same integrity gate, same audit record -- as any other
 * transfer. Nothing here is a second transfer implementation.
 *
 * Two disciplines are deliberate:
 *
 * * a request never carries an absolute path. The only identifiers that travel
 *   are the opaque ``source_ref`` / ``source_version`` and the server's own
 *   command id, which is also what makes a replayed command idempotent rather
 *   than a second copy;
 * * a refusal keeps the *server's* code. Mapping a real ``chunk_conflict`` or
 *   ``limit_exceeded`` onto a generic failure would hide from the user why the
 *   file did not go.
 *
 * The origin is validated here rather than trusted from a caller: no userinfo,
 * no path, no query -- the same rule the device gateway handshake follows.
 */

import {
  TransferError,
  DEFAULT_CHUNK_SIZE,
  type ChunkPoster,
  type TransferCommit,
  type TransferCreate,
} from '../local-files/transfer'
import type { MaterializeTransport } from '../local-files/materialize'

export type { MaterializeTransport }

export interface MaterializeTransportOptions {
  /** The configured server origin; the request URLs are derived from it. */
  origin: string
  /** Native bearer, read per call so a reconnect uses the current session. */
  token: () => Promise<string | null>
  /** Current tenant from the native broker, never a model-supplied argument. */
  tenantId: () => string | null
  /** Injected so tests can answer without a socket. */
  fetch: typeof fetch
  /** Allow plain http for this exact origin only (the bundled loopback case). */
  allowHttpOrigin?: string
  timeoutMs?: number
}

const DEFAULT_TIMEOUT_MS = 120_000

export const TRANSFER_CREATE_PATH = '/api/desktop/transfers'

/** Validate and normalise the origin: an origin, and nothing else. */
export function materializeBaseUrl(origin: string, allowHttpOrigin?: string): string {
  let parsed: URL
  try {
    parsed = new URL(origin)
  } catch {
    throw new TransferError('invalid_request', 'the configured origin is not a URL')
  }
  let allowed = parsed.protocol === 'https:'
  if (parsed.protocol === 'http:') {
    const allowedHost = allowHttpOrigin ? safeHost(allowHttpOrigin) : ''
    allowed = !!allowedHost && allowedHost === parsed.host && isLoopbackHost(parsed.hostname)
  }
  if (!allowed) {
    throw new TransferError('invalid_request', 'the configured origin must be https')
  }
  if (parsed.username || parsed.password) {
    throw new TransferError('invalid_request', 'the origin must not carry userinfo')
  }
  if (parsed.pathname && parsed.pathname !== '/') {
    throw new TransferError('invalid_request', 'the origin must not carry a path')
  }
  if (parsed.search || parsed.hash) {
    throw new TransferError('invalid_request', 'the origin must not carry a query')
  }
  return parsed.origin
}

function safeHost(candidate: string): string {
  try {
    return new URL(candidate).host
  } catch {
    return ''
  }
}

function isLoopbackHost(hostname: string): boolean {
  return hostname === '127.0.0.1' || hostname === 'localhost' || hostname === '::1'
}

/** The error body the desktop handlers send: ``{status, message, code}``. */
function codeFromBody(body: unknown): { code: string; message: string } | null {
  if (!body || typeof body !== 'object') return null
  const record = body as Record<string, unknown>
  const code = typeof record.code === 'string' ? record.code.trim() : ''
  const message = typeof record.message === 'string' ? record.message : ''
  if (!code) return null
  return { code, message: message || code }
}

/** ``{status:"success", data:{...}}`` -- anything else is not an answer. */
function dataFromBody(body: unknown): Record<string, unknown> {
  if (!body || typeof body !== 'object') {
    throw new TransferError('transport_error', 'the server sent no transfer body')
  }
  const record = body as Record<string, unknown>
  const data = record.data
  if (!data || typeof data !== 'object') {
    throw new TransferError('transport_error', 'the server sent no transfer data')
  }
  return data as Record<string, unknown>
}

function asNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.trunc(value)
  return null
}

/** Build the transport production uses for ``materialize``. */
export function createMaterializeTransport(
  options: MaterializeTransportOptions,
): MaterializeTransport {
  const base = materializeBaseUrl(options.origin, options.allowHttpOrigin)
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS

  async function send(
    path: string,
    init: {
      method: string
      json?: unknown
      body?: Uint8Array
      headers?: Record<string, string>
    },
  ): Promise<Record<string, unknown>> {
    const token = await options.token()
    if (!token) {
      throw new TransferError('auth_required', 'this device has no session for the server')
    }
    const tenantId = options.tenantId()
    if (!tenantId) throw new TransferError('missing_tenant', 'no tenant is selected for this transfer')
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    let res: Response
    try {
      res = await options.fetch(`${base}${path}`, {
        method: init.method,
        redirect: 'error',
        signal: controller.signal,
        headers: {
          Authorization: `Bearer ${token}`,
          'X-Tenant-ID': tenantId,
          ...(init.json === undefined ? {} : { 'Content-Type': 'application/json' }),
          ...(init.headers || {}),
        },
        ...(init.body === undefined
          ? init.json === undefined
            ? {}
            : { body: JSON.stringify(init.json) }
          : { body: init.body as unknown as string }),
      })
    } catch (err) {
      // A transport failure is not a server refusal: keep them distinguishable,
      // and never surface the URL (a fetch error can echo the request).
      const aborted = (err as { name?: string } | null)?.name === 'AbortError'
      throw new TransferError(
        aborted ? 'deadline_exceeded' : 'transport_error',
        aborted
          ? 'the transfer request timed out'
          : 'the transfer request could not reach the server',
      )
    } finally {
      clearTimeout(timer)
    }
    let parsed: unknown = null
    try {
      parsed = await res.json()
    } catch {
      parsed = null
    }
    if (!res.ok) {
      const failure = codeFromBody(parsed)
      throw new TransferError(
        failure?.code || 'transport_error',
        failure?.message || `the transfer request was refused (${res.status})`,
      )
    }
    return dataFromBody(parsed)
  }

  const create: TransferCreate = async (args) => {
    const data = await send(TRANSFER_CREATE_PATH, {
      method: 'POST',
      json: {
        command_id: args.commandId,
        source_ref: args.sourceRef,
        source_version: args.sourceVersion,
        total_bytes: args.totalBytes,
        filename: args.filename,
      },
    })
    const id = typeof data.id === 'string' ? data.id : ''
    if (!id) {
      throw new TransferError('transport_error', 'the server did not open a transfer')
    }
    return {
      id,
      chunk_size: asNumber(data.chunk_size) ?? DEFAULT_CHUNK_SIZE,
      acknowledged_offset: asNumber(data.acknowledged_offset) ?? 0,
    }
  }

  const putChunk: ChunkPoster = async (transferId, offset, body, sha256) => {
    if (!transferId) {
      throw new TransferError('invalid_request', 'a chunk needs the transfer it belongs to')
    }
    const data = await send(
      `${TRANSFER_CREATE_PATH}/${encodeURIComponent(transferId)}/chunks/${offset}`,
      { method: 'PUT', body, headers: { 'X-Content-SHA256': sha256 } },
    )
    const acknowledged = asNumber(data.acknowledged_offset)
    if (acknowledged === null) {
      throw new TransferError('transport_error', 'the server acknowledged no offset')
    }
    return { acknowledged_offset: acknowledged, state: String(data.state || '') }
  }

  const commit: TransferCommit = async (args) => {
    const data = await send(
      `${TRANSFER_CREATE_PATH}/${encodeURIComponent(args.transferId)}/commit`,
      {
        method: 'POST',
        json: {
          total_bytes: args.totalBytes,
          sha256: args.sha256,
          source_version_after: args.sourceVersionAfter,
        },
      },
    )
    return {
      state: String(data.state || ''),
      artifact_ref: typeof data.artifact_ref === 'string' ? data.artifact_ref : undefined,
    }
  }

  return { create, putChunk, commit }
}
