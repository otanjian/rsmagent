// The isolated content window: where a server document or attachment is shown.
//
// Change ``add-desktop-remote-web-workbench``, task 4.4 as carried into 5.6.
// The shell document is the only document that may hold the bridge, so a
// preview, an uploaded file or a rendered HTML attachment must be shown
// *somewhere else* -- otherwise the shell has to navigate to it, which is the
// exact thing the bridge check exists to prevent.
//
// That "somewhere else" is this window: the same partition, so the guest's
// cookies (and therefore the original Web authentication) still apply, but
// **no preload**, so there is no ``window.desktopHost`` to reach; its popups are
// refused, it is granted no device permission, and it may only navigate inside
// the authorized origin. A download it triggers is handled by the same policy
// the container uses (``downloads.ts``).
//
// The pure part -- the web preferences -- is what the tests assert, because a
// forgotten ``preload: undefined`` here would silently hand the bridge to
// user-generated HTML.

import { BrowserWindow, type Session } from 'electron'

export interface ContentWindowPreferences {
  partition: string
  sandbox: boolean
  contextIsolation: boolean
  nodeIntegration: boolean
  nodeIntegrationInSubFrames: boolean
  webSecurity: boolean
  allowRunningInsecureContent: boolean
  /** Absent by construction: there is no bridge in a content document. */
  preload?: never
  /** A content document never opens a native window of its own. */
  nativeWindowOpen: boolean
}

/**
 * The web preferences every content window uses.
 *
 * Deliberately a function (and not an object literal at the call site) so the
 * absence of ``preload`` is one decision, made once, and asserted.
 */
export function contentWindowPreferences(partition: string): ContentWindowPreferences {
  return {
    partition,
    sandbox: true,
    contextIsolation: true,
    nodeIntegration: false,
    nodeIntegrationInSubFrames: false,
    webSecurity: true,
    allowRunningInsecureContent: false,
    nativeWindowOpen: false,
  }
}

export interface ContentWindowOptions {
  url: string
  /** Non-persistent partition of the container that asked for this document. */
  partition: string
  /** The exact authorized origin; navigation outside it is refused. */
  origin: string
  session: Session
  parent?: BrowserWindow
  title?: string
}

/**
 * Open a content document in its own window.
 *
 * Returns the window so the caller can keep it for the container's lifetime and
 * close it on detach. Navigation is pinned to the origin, popups are refused,
 * and nothing is granted: a content document may *display*, and that is all.
 */
export function openContentWindow(options: ContentWindowOptions): BrowserWindow {
  const window = new BrowserWindow({
    width: 960,
    height: 720,
    title: options.title || '预览',
    parent: options.parent,
    show: false,
    webPreferences: contentWindowPreferences(options.partition),
  })

  const contents = window.webContents
  contents.setWindowOpenHandler(() => ({ action: 'deny' }))
  options.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
  contents.on('will-navigate', (event, url) => {
    try {
      if (new URL(url).origin !== new URL(options.origin).origin) event.preventDefault()
    } catch {
      event.preventDefault()
    }
  })
  window.once('ready-to-show', () => window.show())
  void contents.loadURL(options.url)
  return window
}
