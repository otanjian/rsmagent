/**
 * Hand the picked root to the *same-machine* backend (task 5.1).
 *
 * The chain the requirement describes -- native picker, verified root, the
 * original Agent tools acting in that directory -- has one missing link if this
 * call is absent: the backend knows the workspace/binding identifiers, but not
 * where the user's directory is. In remote mode that is the whole point (the
 * path never leaves the machine). In local mode the shell and the backend are
 * the same machine, so the shell tells the backend it started, over loopback,
 * with the launch token only it can present.
 *
 * Three properties this module is responsible for:
 *
 * 1. **Only the local backend is told.** A non-loopback origin is refused here,
 *    so "register my path with the server" is not a thing this code can do even
 *    if a caller passes the wrong origin.
 * 2. **Only when there is something to authorize.** No token, no native bearer,
 *    no absolute path, or a scope missing an identifier means nothing is sent --
 *    the backend refuses those anyway, and sending them would only produce a
 *    404-shaped log line nobody can act on.
 * 3. **The path is never logged.** Refusals carry the backend's own message; the
 *    request body is the one place the absolute path appears, and it travels to
 *    a process on this machine.
 */

import { DESKTOP_TOKEN_HEADER } from './launch-token'

export interface RegisterLocalRootRequest {
  /** The local backend's origin (``http://localhost:<port>``). */
  origin: string
  /** The launch token from :mod:`launch-token`. */
  launchToken: string | null
  /** The native bearer session (not a Web child's cookie). */
  nativeBearer: string | null
  deviceId: string
  workspaceId: string
  bindingId: string
  /** The absolute directory the native picker returned. Main process only. */
  absolutePath: string
  /** ``project-execution`` or ``readonly-input``; never invented here. */
  projectMode: 'project-execution' | 'readonly-input'
  tenantId?: string
  agentId?: string
  businessSessionId?: string
  fetchFn?: typeof fetch
}

export type LocalRootResult =
  | { ok: true; projectMode: string; grantVersion: number }
  | { ok: false; code: string; message: string }

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]'])

/** Whether an origin addresses this machine. */
export function isLoopbackOrigin(origin: string): boolean {
  try {
    const parsed = new URL(origin)
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false
    const host = parsed.hostname.toLowerCase()
    return LOOPBACK_HOSTS.has(host) || host.startsWith('127.')
  } catch {
    return false
  }
}

function refusal(code: string, message: string): LocalRootResult {
  return { ok: false, code, message }
}

/**
 * Register the resolved root with the local backend.
 *
 * The answer is the backend's own: it re-checks loopback, the launch token, the
 * native session and the ownership of the device/workspace/binding triple, and
 * a refusal is reported as-is so the caller can show why the project is not
 * actually open. There is no local "assume success" path.
 */
export async function registerLocalRoot(
  request: RegisterLocalRootRequest,
): Promise<LocalRootResult> {
  if (!isLoopbackOrigin(request.origin)) {
    return refusal('invalid_request', 'only a same-machine backend can be given a local root')
  }
  if (!request.launchToken) {
    return refusal('desktop_token_required', 'this run has no desktop launch token')
  }
  if (!request.nativeBearer) {
    return refusal('permission_denied', 'a native session is required to register a local root')
  }
  for (const [name, value] of [
    ['deviceId', request.deviceId],
    ['workspaceId', request.workspaceId],
    ['bindingId', request.bindingId],
  ] as const) {
    if (!value) return refusal('invalid_request', `${name} is required`)
  }
  if (!request.absolutePath || request.absolutePath.includes('\0')) {
    return refusal('invalid_request', 'an absolute path is required')
  }
  const body = {
    binding_id: request.bindingId,
    device_id: request.deviceId,
    workspace_id: request.workspaceId,
    absolute_path: request.absolutePath,
    project_mode: request.projectMode,
    ...(request.tenantId ? { tenant_id: request.tenantId } : {}),
    ...(request.agentId ? { agent_id: request.agentId } : {}),
    ...(request.businessSessionId ? { business_session_id: request.businessSessionId } : {}),
  }
  const fetchFn = request.fetchFn ?? fetch
  let response: Response
  try {
    response = await fetchFn(`${request.origin.replace(/\/+$/, '')}/api/desktop/local-roots`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${request.nativeBearer}`,
        [DESKTOP_TOKEN_HEADER]: request.launchToken,
      },
      body: JSON.stringify(body),
    })
  } catch (err) {
    return refusal('backend_unavailable', String((err as Error)?.message || err))
  }
  const text = await response.text().catch(() => '')
  let payload: Record<string, unknown> = {}
  try {
    payload = text ? (JSON.parse(text) as Record<string, unknown>) : {}
  } catch {
    payload = {}
  }
  if (!response.ok) {
    const message = typeof payload.message === 'string' && payload.message
      ? payload.message
      : `the local backend refused the root (HTTP ${response.status})`
    const code = typeof payload.code === 'string' && payload.code ? payload.code : 'invalid_request'
    return refusal(code, message)
  }
  return {
    ok: true,
    projectMode: String(payload.project_mode || request.projectMode),
    grantVersion: Number(payload.grant_version || 0),
  }
}

export interface BindSessionTargetRequest {
  origin: string
  launchToken: string | null
  nativeBearer: string | null
  agentId: string
  sessionId: string
  deviceId: string
  workspaceId: string
  bindingId: string
  projectMode?: 'project-execution' | 'readonly-input'
  tenantId?: string
  fetchFn?: typeof fetch
}

/**
 * Point one chat at the registered project.
 *
 * Separate from the registration because they answer different questions -- "the
 * backend may use this directory" versus "this conversation runs in it" -- and
 * the second one is what makes a turn's tools land in the project.
 */
export async function bindSessionTarget(
  request: BindSessionTargetRequest,
): Promise<{ ok: true } | { ok: false; code: string; message: string }> {
  if (!isLoopbackOrigin(request.origin)) {
    return refusal('invalid_request', 'only a same-machine backend can be bound locally')
  }
  if (!request.launchToken) {
    return refusal('desktop_token_required', 'this run has no desktop launch token')
  }
  if (!request.nativeBearer) {
    return refusal('permission_denied', 'a native session is required to bind a local project')
  }
  for (const [name, value] of [
    ['agentId', request.agentId], ['sessionId', request.sessionId],
    ['deviceId', request.deviceId], ['workspaceId', request.workspaceId],
    ['bindingId', request.bindingId],
  ] as const) {
    if (!value) return refusal('invalid_request', `${name} is required`)
  }
  const fetchFn = request.fetchFn ?? fetch
  let response: Response
  try {
    response = await fetchFn(
      `${request.origin.replace(/\/+$/, '')}/api/desktop/sessions/${encodeURIComponent(request.sessionId)}/execution-target`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${request.nativeBearer}`,
          [DESKTOP_TOKEN_HEADER]: request.launchToken,
        },
        body: JSON.stringify({
          agent_id: request.agentId,
          binding_id: request.bindingId,
          device_id: request.deviceId,
          workspace_id: request.workspaceId,
          ...(request.projectMode ? { project_mode: request.projectMode } : {}),
          ...(request.tenantId ? { tenant_id: request.tenantId } : {}),
        }),
      },
    )
  } catch (err) {
    return refusal('backend_unavailable', String((err as Error)?.message || err))
  }
  const text = await response.text().catch(() => '')
  if (response.ok) return { ok: true }
  let payload: Record<string, unknown> = {}
  try {
    payload = text ? (JSON.parse(text) as Record<string, unknown>) : {}
  } catch {
    payload = {}
  }
  return refusal(
    typeof payload.code === 'string' && payload.code ? payload.code : 'invalid_request',
    typeof payload.message === 'string' && payload.message
      ? payload.message
      : `the local backend refused the session target (HTTP ${response.status})`,
  )
}

/** Tell the backend to forget a root (a closed project, a revoked grant). */
export async function clearLocalRoot(request: {
  origin: string
  launchToken: string | null
  nativeBearer: string | null
  deviceId: string
  workspaceId?: string
  bindingId?: string
  fetchFn?: typeof fetch
}): Promise<{ ok: boolean; code?: string; message?: string }> {
  if (!isLoopbackOrigin(request.origin) || !request.launchToken || !request.nativeBearer) {
    return { ok: false, code: 'invalid_request', message: 'a loopback origin and the desktop launch token are required' }
  }
  const fetchFn = request.fetchFn ?? fetch
  try {
    const response = await fetchFn(
      `${request.origin.replace(/\/+$/, '')}/api/desktop/local-roots`,
      {
        method: 'DELETE',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${request.nativeBearer}`,
          [DESKTOP_TOKEN_HEADER]: request.launchToken,
        },
        body: JSON.stringify({
          device_id: request.deviceId,
          ...(request.workspaceId ? { workspace_id: request.workspaceId } : {}),
          ...(request.bindingId ? { binding_id: request.bindingId } : {}),
        }),
      },
    )
    return { ok: response.ok }
  } catch (err) {
    return { ok: false, code: 'backend_unavailable', message: String((err as Error)?.message || err) }
  }
}
