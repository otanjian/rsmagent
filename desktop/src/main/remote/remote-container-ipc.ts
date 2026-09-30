// Attaching and detaching the remote container from the local shell.
//
// Change ``add-desktop-remote-web-workbench``, tasks 4.1/4.6. This is the only
// place the container is created and destroyed, so the two rules that matter
// have one implementation each:
//
//   * nothing is loaded until the native parent exists *and* the paired child
//     cookie is installed in the container's own partition -- the page must
//     never render before its requests would be authorized;
//   * stopping is "block first, revoke second": the container and the bridge are
//     torn down locally before the server-side revocation is attempted, so a
//     failed revoke cannot leave a page that still talks to the server.

import { BrowserWindow, app, ipcMain, session as electronSession, shell } from 'electron'
import { randomBytes } from 'crypto'
import * as path from 'path'
import {
  beginAuthorization, bootstrapWebSession, clearWebChildSessions, logout, status,
  getLocalBackendOrigin, nativeBearer,
} from '../auth-broker'
import { createRemoteContainer, type RemoteContainer } from './container'
import { asCookieFetchSession, containerBindingTransport } from './container-binding-transport'
import { openContentWindow } from './content-window'
import { asDownloadSession, installDownloadPolicy } from './downloads'
import { probeServer } from './connection'
import { setRemoteLocalFilesEnabled } from './local-files-bridge'
import { remoteGrantRegistry } from './local-files-bridge'
import { LocalReadAssembly } from './local-read-assembly'
import { resolveGuardBinary, spawnGuardProcess } from '../local-files/fs-guard-binary'
import { registerRemoteHost, setupRemoteHostIPC, teardownRemoteHostIPC } from './remote-host-ipc'
import { remoteCoveringScript } from './shell-covering'
import { partitionName, isValidInstanceId } from './web-session'

/** Tell the local shell to release drag/pointer capture while a guest covers it. */
async function setShellCovered(window: BrowserWindow, covered: boolean): Promise<void> {
  if (window.isDestroyed()) return
  try {
    await window.webContents.executeJavaScript(remoteCoveringScript(covered), true)
  } catch {
    /* renderer may be mid-reload; a later attach/detach retries */
  }
}

/** Channels the local (trusted) shell uses to attach/detach the container. */
export const CHANNEL_START = 'remote-container-start'
export const CHANNEL_STOP = 'remote-container-stop'

let container: RemoteContainer | null = null
let attachedPartition = ''
/** Window whose shell is covered by the active container (for drag passthrough). */
let attachedWindow: BrowserWindow | null = null
/** Disposers for what was installed on the current partition's session. */
let partitionDisposers: (() => void)[] = []
/** Content windows opened by this container; closed when it detaches. */
let contentWindows: BrowserWindow[] = []

/**
 * The local read path for the live container (change
 * ``fix-desktop-local-context-and-tool-calls``, task 2.4): the helper process,
 * the workspace map, and the device connection.
 *
 * Created per attach and disposed on detach, so the device is reachable exactly
 * while a container is bound to a directory and nothing survives a mode switch.
 */
let localRead: LocalReadAssembly | null = null

/** A diagnostics-only report of the device connection state. */
function noteDeviceState(state: string, detail?: string): void {
  if (state === 'ready') console.log('[desktop] device connection ready')
  else if (state === 'reconnecting') console.warn(`[desktop] device connection reconnecting: ${detail || ''}`)
  else if (state === 'stopped') console.log('[desktop] device connection stopped')
}

/** The preload path of the compiled narrow bridge. */
function bridgePreloadPath(): string {
  // ``remote-preload.ts`` is compiled next to ``index.js`` (``dist/main/``),
  // not next to this file (``dist/main/remote/``). The bare ``./remote-preload.js``
  // this used to return pointed at a path that does not exist, and Electron
  // loads a missing preload *silently*: the container rendered the console with
  // no bridge at all, so ``window.desktopHost`` was undefined on the page.
  return path.join(__dirname, '..', 'remote-preload.js')
}

/**
 * Show a same-origin server document or attachment in its own window.
 *
 * Kept next to the container so the two are created and destroyed together: a
 * content window left behind after a detach would be a page with the guest's
 * cookies and no owner. The window has **no preload** -- see
 * ``content-window.ts`` -- which is what keeps the bridge out of
 * user-generated HTML (task 4.4).
 */
function openContent(options: {
  url: string
  kind?: 'document' | 'attachment'
  partition: string
  origin: string
  parent: BrowserWindow
}): void {
  const window = openContentWindow({
    url: options.url,
    partition: options.partition,
    origin: options.origin,
    session: electronSession.fromPartition(options.partition),
    parent: options.parent,
    title: options.kind === 'attachment' ? '文件' : '预览',
  })
  contentWindows.push(window)
  window.once('closed', () => {
    contentWindows = contentWindows.filter((entry) => entry !== window)
  })
}

/**
 * Install the phase-1 "save as" policy on the container's partition.
 *
 * The destination directory is the OS download directory -- chosen by the host,
 * never by the page -- and an existing file is never replaced without an
 * explicit confirmation recorded in ``downloads.ts``.
 */
function installDownloads(options: { partition: string; origin: string; entryPaths: string[] }): void {
  const target = electronSession.fromPartition(options.partition)
  const dispose = installDownloadPolicy({
    session: asDownloadSession(target),
    origin: options.origin,
    entryPaths: options.entryPaths,
    directory: app.getPath('downloads'),
  })
  partitionDisposers.push(dispose)
}

export function remoteContainerState(): { attached: boolean; partition: string } {
  return { attached: container !== null, partition: attachedPartition }
}

/** True when a remote (or local-auto-bound) container is currently attached. */
export function isRemoteContainerAttached(): boolean {
  return container !== null
}

/**
 * Attach: native authorization, then the paired child cookie, then the view.
 *
 * ``origin`` comes from the caller's own profile resolution (the probe result),
 * never from a page, and the server compares it against the origin it stamped
 * when the native session was minted.
 */
export async function attachRemoteContainer(options: {
  window: BrowserWindow
  origin: string
  entryPaths: string[]
  openExternal: (url: string) => void
}): Promise<{ ok: boolean; code?: string; message?: string }> {
  if (container) return { ok: true }
  if (!status().session) {
    // The main process owns authorization; the page cannot start it for itself.
    await beginAuthorization()
  }
  const instanceId = randomBytes(24).toString('base64url')
  if (!isValidInstanceId(instanceId)) {
    return { ok: false, code: 'invalid_request', message: 'could not generate an instance id' }
  }
  const child = await bootstrapWebSession(instanceId)
  installDownloads({
    partition: child.partition,
    origin: options.origin,
    entryPaths: options.entryPaths,
  })
  const view = createRemoteContainer({
    window: options.window,
    partition: child.partition,
    origin: options.origin,
    preloadPath: bridgePreloadPath(),
    entryPaths: options.entryPaths,
    openExternal: options.openExternal,
    openContent: (url, kind) => openContent({
      url, kind, partition: child.partition, origin: options.origin, parent: options.window,
    }),
    session: electronSession.fromPartition(child.partition),
  })
  container = view
  attachedPartition = child.partition
  attachedWindow = options.window
  // The local read path for this container. The helper binary is resolved once
  // here: a build without it keeps the binding flow working but reports that
  // this machine cannot serve files, instead of failing at the first read.
  const guardBinary = resolveGuardBinary({
    resourcesPath: (process as { resourcesPath?: string }).resourcesPath,
    appPath: app.getAppPath(),
  })
  localRead = new LocalReadAssembly({
    registry: remoteGrantRegistry,
    spawnGuard: guardBinary
      ? () => spawnGuardProcess(guardBinary)
      : () => {
        throw new Error('the local file helper (fs-guard) is not installed in this build')
      },
    token: async () => (status().session ? nativeBearer() : null),
    registeredLocalOrigin: getLocalBackendOrigin,
    onState: noteDeviceState,
  })
  // Mirror the server's public meta so the bridge can open chooseWorkspace
  // only when ``features.local_files`` is available (never invent it locally).
  try {
    const probed = await probeServer(options.origin, fetch, {
      allowHttpOrigin: options.origin,
    })
    setRemoteLocalFilesEnabled(
      !!(probed.ok && probed.meta.features.local_files?.available),
    )
  } catch {
    setRemoteLocalFilesEnabled(false)
  }
  registerRemoteHost({
    webContents: view.webContents,
    localFiles: {
      bound: (bound) => {
        // The confirmation named a server workspace and a local grant; hand
        // both to the read path so a command for that workspace can run.
        if (!localRead) return
        if (!localRead.bindDevice(bound)) {
          throw new Error('the local directory could not be attached to the device connection')
        }
      },
      revoked: (all) => {
        if (!localRead) return
        if (all) localRead.dispose()
      },
    },
    // Read live, per call: the registered frame ids and the document generation
    // only exist after the first navigation has committed, and they change on
    // every later one -- a snapshot taken here would refuse the shell itself.
    context: () => ({
      webContents: view.shellFrame,
      shellFrame: view.shellFrame,
      origin: options.origin,
      generation: view.generation,
      entryPaths: options.entryPaths,
    }),
    emit: (payload) => view.webContents.send('desktop:bridge:event', payload),
    openContent: (url, kind) => openContent({
      url, kind, partition: child.partition, origin: options.origin, parent: options.window,
    }),
    // Confirmation of a picked directory needs both credentials: the native
    // bearer (device / workspace / grant) and the container's paired child
    // cookie (the binding itself). See container-binding-transport.ts.
    bindingTransport: containerBindingTransport({
      session: asCookieFetchSession(electronSession.fromPartition(child.partition)),
      origin: options.origin,
      tenantId: status().session?.tenantId || '',
    }),
  })
  setupRemoteHostIPC()
  await view.loadShell()
  // Full-bleed guest (TOP_INSET=0): drop the shell's titlebar drag regions so
  // they cannot steal clicks from the guest's own top chrome (bell / tenant /
  // workspace). See shell-covering.ts.
  await setShellCovered(options.window, true)
  return { ok: true }
}

/** Detach: destroy the view and the bridge, then revoke server-side. */
export async function detachRemoteContainer(): Promise<{ ok: boolean; revoked: boolean; message: string }> {
  // 1. Block: no view, no bridge, no partition, no in-flight request.
  const existing = container
  const coveredWindow = attachedWindow
  container = null
  attachedPartition = ''
  attachedWindow = null
  setRemoteLocalFilesEnabled(false)
  // Step 1 also takes the device off the network: a connection that outlived
  // its container could still be handed commands for a workspace whose binding
  // is being revoked (task 2.4). The helper process dies with it, so no root
  // descriptor survives the detach.
  try {
    localRead?.dispose()
  } catch {
    /* the helper may already be gone */
  }
  localRead = null
  teardownRemoteHostIPC()
  if (coveredWindow && !coveredWindow.isDestroyed()) {
    await setShellCovered(coveredWindow, false)
  }
  // A content window carries the guest's cookies; it must not outlive the
  // container that opened it.
  for (const window of contentWindows.splice(0)) {
    try {
      window.destroy()
    } catch {
      /* already gone */
    }
  }
  for (const dispose of partitionDisposers.splice(0)) {
    try {
      dispose()
    } catch {
      /* nothing left to remove */
    }
  }
  try {
    existing?.destroy()
  } catch {
    /* already gone */
  }
  await clearWebChildSessions()
  // 2. Revoke: only now does the shell ask the server to end the session.
  //    If it fails the broker reports it and refuses further business calls,
  //    which is exactly the "do not claim signed out" requirement.
  try {
    const result = await logout()
    return { ok: result.ok, revoked: result.revoked, message: result.message }
  } catch (e) {
    const message = e instanceof Error ? e.message : 'the server did not confirm the sign-out'
    return { ok: false, revoked: false, message }
  }
}

/** Register the two local channels. Guarded by the caller's sender check. */
export function setupRemoteContainerIPC(
  isTrusted: (event: Electron.IpcMainInvokeEvent) => boolean,
  resolve: () => { window: BrowserWindow | null; origin: string; entryPaths: string[] } | null,
): void {
  ipcMain.removeHandler(CHANNEL_START)
  ipcMain.removeHandler(CHANNEL_STOP)

  ipcMain.handle(CHANNEL_START, async (event) => {
    if (!isTrusted(event)) return { ok: false, code: 'permission_denied', message: 'untrusted caller' }
    const target = resolve()
    if (!target || !target.window) {
      return { ok: false, code: 'backend_unavailable', message: 'no window to attach the container to' }
    }
    try {
      return await attachRemoteContainer({
        window: target.window,
        origin: target.origin,
        entryPaths: target.entryPaths,
        openExternal: (url) => {
          void shell.openExternal(url)
        },
      })
    } catch (e) {
      const message = e instanceof Error ? e.message : 'the container could not be attached'
      return { ok: false, code: 'container_failed', message }
    }
  })

  ipcMain.handle(CHANNEL_STOP, async (event) => {
    if (!isTrusted(event)) return { ok: false, code: 'permission_denied', message: 'untrusted caller' }
    try {
      return await detachRemoteContainer()
    } catch (e) {
      const message = e instanceof Error ? e.message : 'the container could not be detached'
      return { ok: false, revoked: false, message }
    }
  })
}
