/**
 * Desktop device connection: URL allow-list, reconnect backoff, frame bound.
 *
 * Change ``add-desktop-remote-web-workbench`` (task 9.5). The main process opens
 * one WSS to the *configured* remote origin's ``/api/desktop/connect`` — never
 * an arbitrary URL the page proposes. Pure Node so the allow-list and backoff
 * can be unit-tested without a live socket.
 */

/** Reconnect delays in seconds (contracts §7), with ±20% jitter applied by
 * {@link nextReconnectDelayMs}. */
export const RECONNECT_SECONDS = [1, 2, 4, 8, 16, 30] as const

export const FRAME_MAX_BYTES = 64 * 1024

/**
 * Build the WSS URL for the device gateway from an exact HTTPS origin.
 *
 * Refuses anything that is not an exact origin (no path, query, userinfo) and
 * never accepts a caller-supplied absolute WS URL — that is how a page would
 * otherwise point the helper at an attacker.
 */
export function deviceConnectUrl(origin: string): string {
  let parsed: URL
  try {
    parsed = new URL(origin)
  } catch {
    throw new Error('invalid origin')
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    throw new Error('origin must be http(s)')
  }
  if (parsed.username || parsed.password) {
    throw new Error('origin must not carry userinfo')
  }
  if (parsed.pathname && parsed.pathname !== '/') {
    throw new Error('origin must not carry a path')
  }
  if (parsed.search || parsed.hash) {
    throw new Error('origin must not carry a query or fragment')
  }
  const wsProtocol = parsed.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${wsProtocol}//${parsed.host}/api/desktop/connect`
}

/** Refuse a page-supplied absolute WS URL. Only {@link deviceConnectUrl} may mint one. */
export function assertNotArbitraryWsUrl(candidate: string): void {
  const lower = candidate.trim().toLowerCase()
  if (lower.startsWith('ws:') || lower.startsWith('wss:')) {
    throw new Error('arbitrary websocket URLs are not accepted')
  }
}

/**
 * Next reconnect delay in ms. ``attempt`` is 0-based; beyond the table the
 * last entry is used. Jitter is ±20%.
 */
export function nextReconnectDelayMs(attempt: number, random: () => number = Math.random): number {
  const index = Math.max(0, Math.min(attempt, RECONNECT_SECONDS.length - 1))
  const base = RECONNECT_SECONDS[index] * 1000
  const jitter = 1 + (random() * 0.4 - 0.2)
  return Math.round(base * jitter)
}

/** Whether a frame body fits the 64 KiB bound. */
export function frameFits(body: string | Uint8Array): boolean {
  const bytes = typeof body === 'string' ? Buffer.byteLength(body, 'utf8') : body.byteLength
  return bytes <= FRAME_MAX_BYTES
}

/** Headers for the native handshake: Bearer only, never a Cookie. */
export function nativeConnectHeaders(token: string): Record<string, string> {
  if (!token) throw new Error('native token required')
  return { Authorization: `Bearer ${token}` }
}

// Re-export the gateway path constant so callers share one source.
export const CONNECT_PATH = '/api/desktop/connect'
