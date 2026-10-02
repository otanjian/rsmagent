/**
 * Confirm a picked local directory with the server.
 *
 * Change ``fix-desktop-local-context-and-tool-calls`` (tasks 2.1/2.2). Picking a
 * directory only creates a *local* grant; nothing about it exists server-side
 * yet, which is why the old flow could show a directory name while every tool
 * call still ran against the server workspace. The four steps that make the
 * selection real are here, in this order, and the caller only publishes the
 * directory to the page after all four succeeded:
 *
 * 1. ``POST /api/desktop/devices`` (native bearer) refreshes *this install's*
 *    device row and returns its **server** id -- the page's opaque
 *    ``deviceId`` is a scope key, not a device the server knows;
 * 2. ``POST /api/desktop/workspaces`` (native) registers the grant version and
 *    a label. The absolute path never leaves this process;
 * 3. ``POST /api/desktop/bindings`` (paired Web child cookie) creates the
 *    binding for this Agent and business session;
 * 4. ``PUT /api/desktop/bindings/{id}/workspaces/{ws}`` (native) attaches the
 *    workspace grant to the binding at the version just registered.
 *
 * The generation / scope / active-grant rules are not re-implemented here --
 * :func:`bindContext` owns them and this module only supplies the poster that
 * performs the four calls, so a page cannot bypass them by going first, and a
 * tenant switch mid-flight is still a ``grant_revoked``.
 *
 * No Electron import: the transports are injected, so the sequence, the error
 * mapping and the "no workspace id, no success" rule are all unit-tested.
 */

import {
  bindContext,
  type BindContextResult,
  type BindingPosterResult,
} from '../local-files/bind-context'
import type { GrantPublic, GrantScope } from '../local-files/grants'

/** One server answer, reduced to what the decision needs. */
export interface HttpReply {
  status: number
  body: string
}

export interface TransportInit {
  method: string
  body?: string
}

/** One credentialed request. ``native`` carries the bearer, ``web`` the cookie. */
export type TransportRequest = (path: string, init: TransportInit) => Promise<HttpReply>

export interface BindingTransport {
  /** Native bearer: device / workspace registration, grant binding. */
  native: TransportRequest
  /** Paired Web child cookie: the binding itself (contracts §4). */
  web: TransportRequest
}

export interface LocalContextSetupRequest {
  /** The page's grant scope. Its ``deviceId`` is an opaque local scope key. */
  scope: GrantScope
  /** The install id the *server* keys its device row by. */
  installationId: string
  /** Display label of the picked directory (never a path). */
  label: string
  /** Grant version the local picker minted. */
  grantVersion: number
  agentId: string
  businessSessionId: string
  contextNonce: string
  /**
   * Document generation this call runs under. Stamped by the main process from
   * its own handshake, never sent by the page: ``bindContext`` compares it
   * against the live generation, so a page that navigates away mid-flight
   * cannot land a binding on the document it left.
   */
  generation: number
  /** Platform for the device row. Filled by the host, never by the page. */
  platform: string
  /** Client version for the device row. Filled by the host, never by the page. */
  clientVersion: string
}

export type LocalContextSetup = BindContextResult

/** A reply the caller must surface verbatim (server ``code`` + message). */
class SetupProblem extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message)
  }
}

function parseReply(reply: HttpReply): { code?: string; message?: string; data?: unknown } {
  try {
    const parsed = JSON.parse(reply.body || '{}')
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

/**
 * Read the ``data`` of a successful reply, or fail with the server's own code.
 *
 * A 2xx that carries no usable payload is a failure, not a success: publishing
 * a directory whose server-side ids we never received would put the page back
 * in the "label only" state this change exists to remove.
 */
function readData(reply: HttpReply, what: string): Record<string, unknown> {
  const parsed = parseReply(reply)
  if (reply.status < 200 || reply.status >= 300) {
    throw new SetupProblem(
      typeof parsed.code === 'string' && parsed.code ? parsed.code : 'invalid_request',
      typeof parsed.message === 'string' && parsed.message ? parsed.message : what,
    )
  }
  const data = parsed.data
  if (!data || typeof data !== 'object') throw new SetupProblem('invalid_request', what)
  return data as Record<string, unknown>
}

function readId(data: Record<string, unknown>, what: string): string {
  const id = data.id
  if (typeof id !== 'string' || !id) throw new SetupProblem('invalid_request', what)
  return id
}

async function ensureDevice(
  transport: BindingTransport,
  request: LocalContextSetupRequest,
): Promise<string> {
  const reply = await transport.native('/api/desktop/devices', {
    method: 'POST',
    body: JSON.stringify({
      installation_id: request.installationId,
      display_name: request.label,
      platform: request.platform,
      client_version: request.clientVersion,
    }),
  })
  return readId(readData(reply, 'the device could not be registered'), 'the server returned no device id')
}

async function registerWorkspaceId(
  transport: BindingTransport,
  deviceId: string,
  label: string,
  grantVersion: number,
): Promise<string> {
  const reply = await transport.native('/api/desktop/workspaces', {
    method: 'POST',
    body: JSON.stringify({
      device_id: deviceId,
      label,
      grant_version: grantVersion,
    }),
  })
  return readId(
    readData(reply, 'the local directory could not be registered'),
    'the server returned no workspace id',
  )
}

async function attachWorkspace(
  transport: BindingTransport,
  bindingId: string,
  workspaceId: string,
  grantVersion: number,
): Promise<void> {
  const reply = await transport.native(
    `/api/desktop/bindings/${encodeURIComponent(bindingId)}/workspaces/${encodeURIComponent(workspaceId)}`,
    { method: 'PUT', body: JSON.stringify({ grant_version: grantVersion }) },
  )
  readData(reply, 'the local directory could not be attached to the session')
}

/**
 * The four server calls, as one poster for :func:`bindContext`.
 *
 * Device and workspace are resolved *before* the binding is created: a binding
 * without a workspace is exactly the half-confirmed state that must never reach
 * the page.
 */
export function localSetupPoster(
  request: LocalContextSetupRequest,
  transport: BindingTransport,
): (body: {
  device_id: string
  agent_id: string
  business_session_id: string
  context_nonce: string
}) => Promise<BindingPosterResult> {
  return async (body) => {
    const deviceId = await ensureDevice(transport, request)
    const workspaceId = await registerWorkspaceId(
      transport,
      deviceId,
      request.label,
      request.grantVersion,
    )
    const created = readData(
      await transport.web('/api/desktop/bindings', {
        method: 'POST',
        body: JSON.stringify({
          device_id: deviceId,
          agent_id: body.agent_id,
          business_session_id: body.business_session_id,
          context_nonce: body.context_nonce,
        }),
      }),
      'the session could not be bound to the local directory',
    )
    const bindingId = readId(created, 'the server returned no binding id')
    await attachWorkspace(transport, bindingId, workspaceId, request.grantVersion)
    // The device id travels back with the binding: the gateway connection needs
    // the *server's* id, and this is the call that just refreshed the row.
    return { id: bindingId, workspaceId, grantVersion: request.grantVersion, deviceId }
  }
}

/**
 * Confirm the picked directory: check generation / scope / grant, run the four
 * calls, and answer with the reference the chat turn will carry.
 */
export async function confirmLocalContext(
  request: LocalContextSetupRequest,
  liveGeneration: number,
  activeGrant: GrantPublic | null,
  transport: BindingTransport,
): Promise<LocalContextSetup> {
  try {
    return await bindContext(
      {
        // The *grant's* device id is what the page's scope was keyed by; the
        // real server device id is resolved inside the poster.
        deviceId: (activeGrant && activeGrant.deviceId) || request.scope.deviceId,
        agentId: request.agentId,
        businessSessionId: request.businessSessionId,
        contextNonce: request.contextNonce,
        generation: request.generation,
      },
      {
        liveGeneration,
        serverId: request.scope.serverId,
        userId: request.scope.userId,
        tenantId: request.scope.tenantId,
      },
      activeGrant,
      localSetupPoster(request, transport),
    )
  } catch (err) {
    if (err instanceof SetupProblem) {
      return { ok: false, code: err.code, message: err.message }
    }
    const code = (err as { code?: unknown } | null)?.code
    const message = (err as { message?: unknown } | null)?.message
    return {
      ok: false,
      code: typeof code === 'string' && code ? code : 'invalid_request',
      message: typeof message === 'string' && message ? message : 'the local directory could not be confirmed',
    }
  }
}
