// Remote workbench container: the only place a server page is loaded natively.
//
// Change ``add-desktop-remote-web-workbench``, tasks 4.1/4.4/4.5. One
// ``WebContentsView`` per live container, in a non-persistent partition, with
// the bridge preload attached *only* to the shell document. The decision logic
// lives in ``host-bridge.ts`` (pure, tested); this file is the Electron wiring
// and stays deliberately thin so there is little here that a test would have to
// take on faith.

import { WebContentsView, type BaseWindow, type Session, type WebContents } from 'electron'
import {
  BRIDGE_VERSION, checkTopFrameNavigation, permissionVerdict, safeUrl,
  sameOrigin,
} from './host-bridge'

export interface ContainerOptions {
  window: BaseWindow
  /** Non-persistent partition (``partitionName`` from ``web-session.ts``). */
  partition: string
  /** The exact backend origin this container was authorized against. */
  origin: string
  /** Absolute path of the compiled remote-preload script. */
  preloadPath: string
  /** Shell entry paths the server advertised. */
  entryPaths: string[]
  /** Called for a navigation that must leave for the system browser. */
  openExternal: (url: string) => void
  /**
   * Called for a same-origin server document or attachment. It must open a
   * surface *without* the bridge (``openContentWindow``); a container that
   * navigated the shell to it would hand the bridge to user-generated HTML.
   */
  openContent?: (url: string, kind: 'document' | 'attachment') => void
  /** The partition's session, resolved by the caller from ``partition``. */
  session: Session
}

export interface RemoteContainer {
  view: WebContentsView
  webContents: WebContents
  /** The registered shell frame; every IPC call is checked against it. */
  shellFrame: { webContentsId: number; frameProcessId: number; frameRoutingId: number }
  /** Monotonic document generation; bumped on every committed navigation. */
  generation: number
  loadShell(): Promise<void>
  destroy(): void
}

/** Inset below the shell chrome. Full-bleed (0) is intentional for the bound
 *  Web workbench; attach must then clear the parent titlebar drag via
 *  ``shell-covering`` so guest header controls stay clickable. */
const TOP_INSET = 0

/**
 * Create the container view and wire its navigation and permission policy.
 *
 * ``sandbox: true`` + ``contextIsolation: true`` + ``nodeIntegration: false``
 * is the whole point: the page is a browser document, and its only way into the
 * main process is the preload's narrow bridge. ``webSecurity`` and
 * ``allowRunningInsecureContent`` are left at their secure defaults and are
 * asserted in the tests.
 */
export function createRemoteContainer(options: ContainerOptions): RemoteContainer {
  const view = new WebContentsView({
    webPreferences: {
      partition: options.partition,
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      nodeIntegrationInSubFrames: false,
      webSecurity: true,
      allowRunningInsecureContent: false,
      spellcheck: false,
      preload: options.preloadPath,
      // The page must not be able to open a native window of its own: every
      // window it asks for is either refused or handed to the system browser.
      additionalArguments: [],
    },
  })
  const container: RemoteContainer = {
    view,
    webContents: view.webContents,
    shellFrame: { webContentsId: -1, frameProcessId: -1, frameRoutingId: -1 },
    generation: 0,
    async loadShell() {
      await view.webContents.loadURL(options.origin + '/')
    },
    destroy() {
      try {
        view.webContents.close()
      } catch {
        /* already closed */
      }
      try {
        options.window.contentView.removeChildView(view)
      } catch {
        /* not attached */
      }
    },
  }

  options.window.contentView.addChildView(view)
  view.setBounds(options.window.getContentBounds())

  // -- document registration and generation ------------------------------
  //
  // A committed navigation replaces the document. The generation is bumped and
  // the *new* document's frame ids are registered; anything still holding the
  // old generation (a late IPC from the document that was navigated away) is
  // refused by ``checkSender``.
  const registerDocument = () => {
    container.generation += 1
    container.shellFrame = {
      webContentsId: view.webContents.id,
      frameProcessId: view.webContents.mainFrame.processId,
      frameRoutingId: view.webContents.mainFrame.routingId,
    }
  }
  view.webContents.on('did-finish-load', registerDocument)
  view.webContents.on('did-navigate', registerDocument)

  // -- top-frame navigation ------------------------------------------------
  view.webContents.on('will-navigate', (event, url) => {
    const verdict = checkTopFrameNavigation(url, {
      origin: options.origin, entryPaths: options.entryPaths,
    })
    if (verdict.ok) return
    event.preventDefault()
    if (verdict.external) {
      // A page that navigates itself off-origin is doing what a link does; the
      // system browser is the safe answer, never this container.
      const target = safeUrl(url)
      if (target) options.openExternal(target.toString())
      return
    }
    if (verdict.content) {
      // A preview or an attachment: shown in the isolated content window, which
      // has the partition's cookies but no bridge. This is the path a browser
      // takes natively, so the pages keep behaving the same without any change
      // to their own navigation code.
      const target = safeUrl(url)
      if (target) options.openContent?.(target.toString(), verdict.content)
    }
  })

  // -- popups --------------------------------------------------------------
  //
  // Phase 1 allows none: ``window.open`` on a remote page is refused outright,
  // and an off-origin http(s) target is offered to the system browser instead.
  // A same-origin content target is the one case that must not be dropped: it
  // is how the pages open a preview or a raw file (`window.open(preview_url)`),
  // so it goes to the isolated content window exactly as the navigation case
  // above does. No ``setWindowOpenHandler`` result ever carries webPreferences,
  // so a page cannot mint a less-sandboxed window.
  view.webContents.setWindowOpenHandler((details) => {
    const target = safeUrl(details.url)
    if (target && !sameOrigin(details.url, options.origin)
      && (target.protocol === 'http:' || target.protocol === 'https:')
      && !target.username && !target.password) {
      options.openExternal(target.toString())
      return { action: 'deny' }
    }
    const verdict = checkTopFrameNavigation(details.url, {
      origin: options.origin, entryPaths: options.entryPaths,
    })
    if (!verdict.ok && verdict.content && target) {
      options.openContent?.(target.toString(), verdict.content)
    }
    return { action: 'deny' }
  })

  // -- permissions ---------------------------------------------------------
  //
  // Phase 1 grants nothing to a remote page. The synchronous check handler is
  // answered with ``false`` as well, so a page cannot learn a permission state
  // it would be denied.
  options.session.setPermissionRequestHandler((_contents, _permission, callback) => {
    callback(permissionVerdict(_permission) !== 'deny')
  })
  options.session.setPermissionCheckHandler(() => false)

  // -- size synchronisation -------------------------------------------------
  const resize = () => {
    if (view.webContents.isDestroyed()) return
    const bounds = options.window.getContentBounds()
    view.setBounds({ x: 0, y: TOP_INSET, width: bounds.width, height: Math.max(0, bounds.height - TOP_INSET) })
  }
  options.window.on('resize', resize)
  view.webContents.once('destroyed', () => {
    options.window.removeListener('resize', resize)
  })

  return container
}

/** The bridge version this container reports to the page. */
export function containerBridgeVersion(): string {
  return BRIDGE_VERSION
}
