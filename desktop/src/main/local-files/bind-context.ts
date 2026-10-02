/**
 * ``bindContext``: turn a Web-page request into a server-verified binding.
 *
 * Change ``add-desktop-remote-web-workbench`` (task 8.6). The page may propose a
 * device / agent / business session, but the main process is the one that:
 *
 * 1. checks the document generation still matches (a navigated-away page cannot
 *    bind);
 * 2. posts to the server over the paired Web session;
 * 3. refuses to proceed when the tenant / account / server has changed since
 *    the grant was activated.
 *
 * Pure Node so generation and scope checks can be unit-tested without a real
 * BrowserWindow. The HTTP call is injected.
 */

import type { GrantPublic, GrantScope } from './grants'

export interface BindContextRequest {
  deviceId: string
  agentId: string
  businessSessionId: string
  contextNonce: string
  /** Generation the page claims to be running under. */
  generation: number
}

export interface BindContextScope {
  /** Live document generation registered by the main process. */
  liveGeneration: number
  serverId: string
  userId: string
  tenantId: string
}

export type BindContextResult =
  | {
      ok: true
      bindingId: string
      /** The server workspace the picked root was registered as, when known. */
      workspaceId?: string
      /** The grant version the binding was confirmed at, when known. */
      grantVersion?: number
      /**
       * The *server* device id the binding belongs to, when the poster resolved
       * one. Change ``fix-desktop-local-context-and-tool-calls`` (task 2.4): the
       * device connection hello's with this id, so the confirmation that
       * registered the row is the same call that makes the row reachable.
       */
      deviceId?: string
      generation: number
    }
  | { ok: false; code: string; message: string }

/**
 * What the poster resolved: the binding id plus, for the local-directory
 * confirmation path, the workspace and version the server agreed to.
 */
export interface BindingPosterResult {
  id: string
  workspaceId?: string
  grantVersion?: number
  deviceId?: string
}

export type BindingPoster = (body: {
  device_id: string
  agent_id: string
  business_session_id: string
  context_nonce: string
}) => Promise<BindingPosterResult>

/**
 * Verify generation + scope, then create the binding via ``poster``.
 *
 * A mismatched generation is ``stale_context``: the page must not retry with
 * the old nonce. A scope that no longer matches an active grant is
 * ``grant_revoked`` — the user has to pick the directory again.
 */
export async function bindContext(
  request: BindContextRequest,
  scope: BindContextScope,
  activeGrant: GrantPublic | null,
  poster: BindingPoster,
): Promise<BindContextResult> {
  if (request.generation !== scope.liveGeneration) {
    return {
      ok: false,
      code: 'stale_context',
      message: 'the page generation is no longer current',
    }
  }
  if (!request.deviceId || !request.agentId || !request.businessSessionId) {
    return { ok: false, code: 'invalid_request', message: 'missing binding fields' }
  }
  if (!request.contextNonce || request.contextNonce.length < 22) {
    return { ok: false, code: 'invalid_request', message: 'invalid context_nonce' }
  }
  if (!activeGrant) {
    return {
      ok: false,
      code: 'grant_revoked',
      message: 'no active local directory grant',
    }
  }
  // The active grant must still name this server / user / tenant / device. A
  // tenant switch or account switch clears grants (task 8.6 / 8.7); this is the
  // belt-and-braces check for a race with that clear.
  if (
    activeGrant.serverId !== scope.serverId ||
    activeGrant.userId !== scope.userId ||
    activeGrant.tenantId !== scope.tenantId ||
    activeGrant.deviceId !== request.deviceId
  ) {
    return {
      ok: false,
      code: 'grant_revoked',
      message: 'the local grant no longer matches this scope',
    }
  }

  try {
    const created = await poster({
      device_id: request.deviceId,
      agent_id: request.agentId,
      business_session_id: request.businessSessionId,
      context_nonce: request.contextNonce,
    })
    if (!created || typeof created.id !== 'string' || !created.id) {
      return { ok: false, code: 'invalid_request', message: 'server returned no binding id' }
    }
    return {
      ok: true,
      bindingId: created.id,
      // Carried through unchanged: the confirmation path needs the workspace
      // id and grant version to build the per-turn reference, and the older
      // POST-only path simply leaves them undefined.
      workspaceId: created.workspaceId,
      grantVersion: created.grantVersion,
      deviceId: created.deviceId,
      generation: scope.liveGeneration,
    }
  } catch (err: any) {
    const code = (err && err.code) || 'invalid_request'
    const message = (err && err.message) || 'binding failed'
    return { ok: false, code: String(code), message: String(message) }
  }
}

/**
 * Whether a scope change (tenant / account / server) must invalidate grants.
 * Used by the main process when the active profile or the resolved identity
 * flips: every active grant for the *previous* scope is dropped immediately.
 */
export function scopeChanged(before: GrantScope, after: GrantScope): boolean {
  return (
    before.serverId !== after.serverId ||
    before.userId !== after.userId ||
    before.tenantId !== after.tenantId ||
    before.deviceId !== after.deviceId
  )
}
