// The narrow bridge the remote page may use (``window.desktopHost``).
//
// Change ``add-desktop-remote-web-workbench``, task 4.2. This preload runs in
// the *remote* container only; the local React renderer keeps its own,
// wider ``electronAPI`` (``preload.ts``). What matters here is what is absent:
//
//   * no ``ipcRenderer`` and no generic ``invoke`` -- a page cannot name a
//     channel, so it cannot reach a handler that was written for the shell;
//   * no file system, process, shell or credential surface -- ``saveArtifact``
//     carries inline bytes and a plain file name, and the main process asks the
//     user where to put them;
//   * no token, no cookie, no backend origin: the page learns nothing about the
//     session it is running under. Its requests already carry the paired Cookie.
//
// Every call carries the document generation the preload was handed by the main
// process at start-up. The main process refuses a call whose generation is not
// the current one, which is what stops a document that was navigated away from
// (A06's late response) from acting as the shell.

import { contextBridge, ipcRenderer, type IpcRendererEvent } from 'electron'

const CHANNEL_CALL = 'desktop:bridge:call'
const CHANNEL_HELLO = 'desktop:bridge:hello'
const CHANNEL_EVENT = 'desktop:bridge:event'

/** The generation of the document this preload belongs to. */
let generation = 0
let handshake: Promise<void> | null = null

/**
 * Ask the main process to register this document.
 *
 * The main process answers with the generation only if the caller *is* the
 * registered shell main frame; an iframe, a popup or a document from an older
 * navigation is refused here just as it is for every later call.
 */
function ensureHandshake(): Promise<void> {
  if (!handshake) {
    handshake = ipcRenderer
      .invoke(CHANNEL_HELLO)
      .then((reply: { ok: boolean; generation?: number }) => {
        generation = reply && reply.ok && typeof reply.generation === 'number' ? reply.generation : 0
      })
      .catch(() => {
        generation = 0
      })
  }
  return handshake
}

/**
 * The page contract is the *payload*, not the transport.
 *
 * ``remote-host-ipc.dispatch`` answers ``{ok: true, data}`` or ``{ok: false,
 * code, message}`` -- that envelope belongs to IPC and must not leak onto
 * ``window.desktopHost``. Unwrapping here keeps the two sides honest: the page
 * sees exactly what the contract table in ``contracts.md`` describes
 * (``getCapabilities()`` resolves to ``{bridge, methods, generation}``), and a
 * refusal becomes a rejected promise rather than a resolved "success" whose
 * shape the page would have to know about.
 */
async function call(method: string, params?: Record<string, unknown>): Promise<unknown> {
  await ensureHandshake()
  const reply = await ipcRenderer.invoke(CHANNEL_CALL, { method, params: params || {}, generation })
  if (reply && typeof reply === 'object' && 'ok' in reply) {
    if (reply.ok) return reply.data
    const refusal = new Error(typeof reply.message === 'string' ? reply.message : 'the host refused the call')
    ;(refusal as Error & { code?: string }).code = typeof reply.code === 'string' ? reply.code : 'refused'
    throw refusal
  }
  return reply
}

const listeners = new Map<(payload: unknown) => void, (event: IpcRendererEvent, payload: unknown) => void>()

const desktopHost = {
  /** The only version negotiation the page gets before it calls anything. */
  getCapabilities: () => call('getCapabilities'),

  /** Tell the shell that local context (import, drag-drop) is now irrelevant. */
  suspendLocalContext: (reason?: string) => call('suspendLocalContext', { reason: reason || '' }),

  /** Ask the shell to save inline bytes; the shell owns the file dialog. */
  saveArtifact: (name: string, content: string) => call('saveArtifact', { name, content }),

  /** Ask the system browser to open an http(s) link. Never the container. */
  openExternal: (url: string) => call('openExternal', { url }),

  /**
   * Confirm a picked local directory into a usable, server-verified binding.
   *
   * Picking a directory only creates a local grant; the binding and the
   * workspace grant the tools need are created here. The reply is the only
   * thing the page may publish: an unconfirmed directory must never be shown as
   * the active one. Without a host there is nothing to confirm against, so the
   * browser answers honestly instead of throwing.
   */
  bindContext: (params?: Record<string, unknown>) => call('bindContext', params || {}),

  /**
   * Open the trusted native directory picker and activate a grant.
   *
   * ``scope`` is the grant key (server/user/tenant/device); the absolute path
   * never returns to the page.
   */
  chooseWorkspace: (scope?: Record<string, unknown>) =>
    call('chooseWorkspace', { scope: scope || {} }),

  /** Drop the active grant for the given scope (or all). */
  disconnectWorkspace: (scope?: Record<string, unknown>) =>
    call('disconnectWorkspace', { scope: scope || {} }),

  /** Subscribe to shell events (theme, suspend, host notices). */
  onHostEvent: (listener: (payload: unknown) => void) => {
    if (typeof listener !== 'function') return () => undefined
    const wrapped = (_event: IpcRendererEvent, payload: unknown) => listener(payload)
    listeners.set(listener, wrapped)
    ipcRenderer.on(CHANNEL_EVENT, wrapped)
    return () => {
      const existing = listeners.get(listener)
      if (existing) ipcRenderer.removeListener(CHANNEL_EVENT, existing)
      listeners.delete(listener)
    }
  },
}

contextBridge.exposeInMainWorld('desktopHost', desktopHost)

export type DesktopHost = typeof desktopHost
