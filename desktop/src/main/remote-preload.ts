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
  readSapPage: (params: { binding_id: string; read_id: string; view_id: string }) => call('readSapPage', params),
  sapWorkbenchLayout: (params: {action: 'load' | 'save'; ratio?: number; open?: boolean}) => call('sapWorkbenchLayout', params),
  manageSapLogin: (params: { binding_id: string; tenant_id: string; action: string }) => call('manageSapLogin', params),

  /** Tell the shell that local context (import, drag-drop) is now irrelevant. */
  suspendLocalContext: (reason?: string) => call('suspendLocalContext', { reason: reason || '' }),

  /** Ask the shell to save inline bytes; the shell owns the file dialog. */
  saveArtifact: (name: string, content: string) => call('saveArtifact', { name, content }),

  /** Ask the system browser to open an http(s) link. Never the container. */
  openExternal: (url: string) => call('openExternal', { url }),

  /**
   * End this container's account session through the trusted host.
   *
   * The host revokes the native session, tears the container down and tells the
   * local shell to resume its own login/connection entry. It takes no
   * parameters on purpose: the page cannot name an account, carry a token or
   * aim the sign-out anywhere. This document is usually destroyed by the host
   * before the promise settles, so the caller must treat the local shell -- not
   * this reply -- as the authority on the final state.
   */
  signOut: () => call('signOut'),

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
   * never returns to the page. ``purpose`` selects the authorization being
   * asked for: omitted / ``readonly-input`` is the phase-2 read reference,
   * ``project-execution`` is "open my project here" (change
   * ``align-desktop-project-execution-with-master``).
   */
  chooseWorkspace: (scope?: Record<string, unknown>, purpose?: string) =>
    call('chooseWorkspace', purpose
      ? { scope: scope || {}, purpose }
      : { scope: scope || {} }),

  /** Drop the active grant for the given scope (or all). */
  disconnectWorkspace: (scope?: Record<string, unknown>) =>
    call('disconnectWorkspace', { scope: scope || {} }),

  /**
   * The local project this container already has open for one chat (task 9.3).
   *
   * Asked by a page that has just been (re)loaded: its own variables are gone,
   * but the confirmation the host holds is not, and the host re-verifies it --
   * grant, device connection, chat -- before answering. ``state`` is ``live``
   * with the identifiers to resume, or ``stale``/``none``, and a directory is
   * never part of either answer.
   */
  localContext: (params?: Record<string, unknown>) => call('localContext', params || {}),

  /**
   * The local project **source** behind the file panel (task 9.2).
   *
   * These are how the console reads the project the session is bound to on
   * *this* machine. The backend cannot answer for it -- the path exists here, not
   * there -- so the panel asks the shell instead, and the shell resolves the
   * workspace, the grant and the containment. No method accepts or returns a
   * directory: a reply carries the workspace id, a display label and
   * project-relative paths, and nothing else.
   */
  projectSource: (workspaceId: string) => call('projectSource', { workspace_id: workspaceId }),

  /** One page of the project's directory list, relative to its root. */
  projectTree: (params?: Record<string, unknown>) => call('projectTree', params || {}),

  /** Name or text search inside the project. */
  projectSearch: (params?: Record<string, unknown>) => call('projectSearch', params || {}),

  /** Metadata for one project-relative path. */
  projectResolve: (params?: Record<string, unknown>) => call('projectResolve', params || {}),

  /**
   * One *page* of a file's text. Paged on purpose: a preview must never pull a
   * whole large file through the bridge, and the page size is clamped by the
   * shell rather than trusted from the page.
   */
  projectRead: (params?: Record<string, unknown>) => call('projectRead', params || {}),

  /**
   * Save one file the user edited. Travels as a v2 ``write`` frame -- the same
   * authorization, serialisation and journal as the model's own write -- so a
   * read-only project is refused here rather than written around.
   */
  projectWrite: (params?: Record<string, unknown>) => call('projectWrite', params || {}),

  /**
   * The four things only the system can do with a project file (task 9.4).
   *
   * All four take the same `{ workspace_id, path }` and answer with the file's
   * *name* -- never its path, and never a directory. What the page gets back is
   * whether the system acted, or why it could not: a file that is gone, a
   * project whose grant was revoked, and a machine with no application for this
   * kind of file are three different answers, and the panel says so.
   */
  projectOpenFile: (params?: Record<string, unknown>) => call('projectOpenFile', params || {}),

  /** Show the file in the OS file manager. */
  projectRevealFile: (params?: Record<string, unknown>) => call('projectRevealFile', params || {}),

  /** Put the file's real path on this machine's clipboard. */
  projectCopyPath: (params?: Record<string, unknown>) => call('projectCopyPath', params || {}),

  /**
   * Write a copy where the user chooses.
   *
   * `expected_mtime` is the version the panel read; when the file has changed
   * since, the host refuses rather than copying something the user has not
   * seen, and `accept_current` is how the user says "copy what is there now".
   */
  projectSaveFileAs: (params?: Record<string, unknown>) => call('projectSaveFileAs', params || {}),

  /**
   * The isolated preview of a project file (task 9.5).
   *
   * The answer is a refusal, or a **short-lived** URL to embed (`url`,
   * `expires_at`) plus the file's name and kind. The URL is single-file and
   * script-inert: the frame it is loaded into is sandboxed by the response's own
   * CSP, and the page cannot read the bytes back. A local file's *path* is never
   * part of the answer, and neither is a permanent URL of any kind.
   */
  projectPreviewFile: (params?: Record<string, unknown>) => call('projectPreviewFile', params || {}),

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
