/**
 * The two credentialed transports ``bindContext`` needs, built for a container.
 *
 * Change ``fix-desktop-local-context-and-tool-calls`` (task 2.2). The four
 * confirmation calls split by credential, and neither half may be swapped for
 * the other:
 *
 *   * the device / workspace / grant calls are **native bearer** calls, made by
 *     the main process through the auth broker (the page never holds that
 *     token);
 *   * the binding call is a **paired Web child cookie** call -- the server
 *     refuses a binding create from anything else -- so it is made with the
 *     container session, whose cookie jar is the only place the child secret
 *     exists.
 *
 * The cookie call is also a state-changing request, so the server applies the
 * ordinary origin/CSRF rule for cookie credentials. Chromium's fetch does not
 * let a caller set ``Origin`` from script, so the header is injected at the
 * network layer for exactly this one request and removed again in ``finally``:
 * a listener that outlived the call would rewrite the origin of every later
 * request in the partition.
 */

import type { Session } from 'electron'

import { requestBusiness } from '../auth-broker'
import type { BindingTransport, HttpReply, TransportInit } from './binding-setup'

/** The one endpoint that must carry the paired Web child cookie. */
const BINDING_PATH = '/api/desktop/bindings'

/** The narrow slice of an Electron session this module uses. */
export interface CookieFetchSession {
  fetch: (url: string, init: Record<string, unknown>) => Promise<{
    status: number
    text: () => Promise<string>
  }>
  webRequest: {
    onBeforeSendHeaders: (
      filter: { urls: string[] },
      listener:
        | null
        | ((
            details: { requestHeaders: Record<string, string> },
            callback: (response: { requestHeaders: Record<string, string> }) => void,
          ) => void),
    ) => void
  }
}

export interface ContainerBindingOptions {
  session: CookieFetchSession
  origin: string
  tenantId: string
  /** Native bearer transport; defaults to the auth broker's own. */
  native?: (path: string, init: TransportInit) => Promise<HttpReply>
}

/**
 * Headers a cookie-authenticated binding create must carry.
 *
 * ``Origin`` satisfies the server's origin/CSRF rule for cookie writes;
 * ``X-Tenant-ID`` is the tenant selection every desktop route requires. Both
 * describe the *host's* view of the request, never the page's.
 */
export function bindingRequestHeaders(origin: string, tenantId: string): Record<string, string> {
  return {
    Origin: origin,
    Referer: `${origin.replace(/\/+$/, '')}/`,
    'X-Tenant-ID': tenantId,
    'Content-Type': 'application/json',
    Accept: 'application/json',
  }
}

/**
 * POST one binding create with the container's own cookie jar.
 *
 * ``credentials: 'include'`` is explicit: the child secret must travel, and a
 * default that silently omitted it would turn a usable device into a
 * ``permission_denied`` the user cannot explain.
 */
export async function postBindingWithCookie(options: {
  session: CookieFetchSession
  origin: string
  tenantId: string
  url: string
  body: string
}): Promise<HttpReply> {
  const filter = { urls: [`${options.origin.replace(/\/+$/, '')}${BINDING_PATH}`] }
  const headers = bindingRequestHeaders(options.origin, options.tenantId)
  const inject = (
    details: { requestHeaders: Record<string, string> },
    callback: (response: { requestHeaders: Record<string, string> }) => void,
  ): void => {
    callback({ requestHeaders: { ...details.requestHeaders, ...headers } })
  }
  options.session.webRequest.onBeforeSendHeaders(filter, inject)
  try {
    const reply = await options.session.fetch(options.url, {
      method: 'POST',
      headers,
      body: options.body,
      credentials: 'include',
      redirect: 'error',
    })
    return { status: reply.status, body: await reply.text() }
  } finally {
    options.session.webRequest.onBeforeSendHeaders(filter, null)
  }
}

/** Both transports for one attached container. */
export function containerBindingTransport(
  options: ContainerBindingOptions,
): BindingTransport {
  const native =
    options.native ??
    (async (path: string, init: TransportInit) => {
      const reply = await requestBusiness({ path, method: init.method, body: init.body })
      return { status: reply.status, body: reply.body }
    })
  const base = options.origin.replace(/\/+$/, '')
  return {
    native,
    web: (path: string, init: TransportInit) =>
      postBindingWithCookie({
        session: options.session,
        origin: options.origin,
        tenantId: options.tenantId,
        url: `${base}${path}`,
        body: init.body || '',
      }),
  }
}

/** Narrow a real Electron session to the slice above. */
export const asCookieFetchSession = (session: Session): CookieFetchSession =>
  session as unknown as CookieFetchSession
