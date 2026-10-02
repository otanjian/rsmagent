// Auto-bind the in-process local backend into the remote Web container.
//
// Spec ``desktop-remote-web-workbench``: remote servers are HTTPS, with one
// exception — "本地 HTTP 例外仅限本进程登记的后端". When the capability is
// available and the user has completed native sign-in in *local* mode, the
// workbench MUST attach to that registered origin without asking them to type
// a server URL. Stored remote profiles stay HTTPS-only; this path never
// writes the local origin into ``desktop-remote.json``.

import { shell, type BrowserWindow } from 'electron'

import { getLocalBackendOrigin, status } from '../auth-broker'
import { probeServer } from './connection'
import { startupMode } from './config-ipc'
import { attachRemoteContainer, isRemoteContainerAttached } from './remote-container-ipc'

let boundWindow: BrowserWindow | null = null
let bindInFlight: Promise<boolean> | null = null

/** Remember the shell window auto-bind should target. */
export function setLocalWebBindWindow(window: BrowserWindow | null): void {
  boundWindow = window
}

/**
 * If local mode + registered backend + native session + remote_web available,
 * attach the Web container to the local origin. Idempotent and best-effort.
 */
export async function tryAutoBindLocalWeb(): Promise<boolean> {
  if (bindInFlight) return bindInFlight
  bindInFlight = (async () => {
    try {
      return await runAutoBind()
    } finally {
      bindInFlight = null
    }
  })()
  return bindInFlight
}

async function runAutoBind(): Promise<boolean> {
  if (isRemoteContainerAttached()) return true
  if (startupMode().mode !== 'local') return false
  const origin = getLocalBackendOrigin()
  if (!origin) return false
  if (!status().session) return false
  const window = boundWindow
  if (!window || window.isDestroyed()) return false

  const probed = await probeServer(origin, fetch, { allowHttpOrigin: origin })
  if (!probed.ok) {
    console.warn(`[remote] local web auto-bind probe failed: ${probed.failure.code}`)
    return false
  }
  if (!probed.meta.remote_web.available) {
    // Capability closed: stay on the native React shell (correct unavailable).
    return false
  }

  const attached = await attachRemoteContainer({
    window,
    origin,
    entryPaths: probed.meta.console_entry_paths || [],
    openExternal: (url) => {
      void shell.openExternal(url)
    },
  })
  if (!attached.ok) {
    console.warn(`[remote] local web auto-bind attach failed: ${attached.code || attached.message}`)
    return false
  }
  console.log(`[remote] local web auto-bound to ${origin}`)
  return true
}
