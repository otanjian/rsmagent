/**
 * Diagnostics export for the desktop shell (task 14.4).
 *
 * Pure: strips tokens / cookies / absolute paths / file bodies before a user
 * exports a support bundle. Driven by ``tests/test_desktop_diagnostics.cjs``.
 */

export interface DiagnosticsInput {
  appVersion: string
  bridgeVersion: string
  protocols?: Record<string, { major: number; minor: number }>
  connectionCode?: string
  correlationIds?: string[]
  /** Anything that might contain secrets — scrubbed before export. */
  notes?: string
}

const SECRET_KEYS = [
  'token', 'access_token', 'authorization', 'cookie', 'code', 'bootstrap',
  'password', 'secret', 'api_key', 'apikey',
]

export function scrubDiagnosticsText(raw: string): string {
  let text = String(raw || '')
  // Absolute unix / windows paths → redacted.
  text = text.replace(/\/(?:Users|home|var|tmp)\/[^\s"'`]+/g, '[path]')
  text = text.replace(/[A-Za-z]:\\[^\s"'`]+/g, '[path]')
  for (const key of SECRET_KEYS) {
    const re = new RegExp(`(${key}\\s*[:=]\\s*)([^\\s,;}]+)`, 'gi')
    text = text.replace(re, '$1[redacted]')
  }
  // Bearer headers / long opaque tokens.
  text = text.replace(/Bearer\s+[A-Za-z0-9._\-+/=]+/gi, 'Bearer [redacted]')
  return text
}

export function buildDiagnosticsBundle(input: DiagnosticsInput): {
  exportedAt: string
  appVersion: string
  bridgeVersion: string
  protocols: Record<string, { major: number; minor: number }>
  connectionCode: string
  correlationIds: string[]
  notes: string
} {
  return {
    exportedAt: new Date().toISOString(),
    appVersion: String(input.appVersion || ''),
    bridgeVersion: String(input.bridgeVersion || ''),
    protocols: input.protocols || {},
    connectionCode: String(input.connectionCode || ''),
    correlationIds: (input.correlationIds || []).map(String).slice(0, 32),
    notes: scrubDiagnosticsText(input.notes || ''),
  }
}

/**
 * Protocol negotiation (task 14.1): unknown required major blocks; unknown
 * optional capabilities stay closed and never grant new actions.
 */
export function negotiateProtocol(
  local: { major: number; minor: number; required?: boolean },
  remote: { major: number; minor: number },
): { ok: boolean; reason?: string; minor: number } {
  if (local.required !== false && remote.major !== local.major) {
    return { ok: false, reason: 'incompatible_major', minor: 0 }
  }
  return { ok: true, minor: Math.min(local.minor, remote.minor) }
}
