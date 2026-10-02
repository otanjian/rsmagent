// Desktop remote-mode configuration: the local store, service and IPC surface.
//
// Change ``add-desktop-remote-web-workbench`` (tasks 2.2/2.3). This is the
// trusted *local* shell's half of remote mode: choose and validate a server,
// probe it, switch mode, and switch back to local. It deliberately owns no
// credential and no Web page -- it only reads and writes the versioned config
// (``profiles.ts``) and asks the server for its public metadata
// (``connection.ts``).
//
// Every channel is behind the same trusted-sender check the broker uses
// (``isTrustedWindowFrame``): the remote *Web* container never reaches these
// channels, because its page is not the registered shell document and its
// preload (``remote-preload.ts``) exposes an entirely different, narrower API.
//
// One active server, one account: setting a profile active is the only way to
// have a remote target, and switching mode/profile is what discards the old
// scope -- the container teardown itself lands in group 4.

import { app, ipcMain } from 'electron'
import * as path from 'path'
import type { BrowserWindow } from 'electron'

import { getLocalBackendOrigin, isTrustedWindowFrame, setBackendOrigin } from '../auth-broker'
import { canHonourRemoteMode } from './container-support'
import { probeServer, type ProbeResult } from './connection'
import {
  activeProfile,
  addServer,
  defaultConfig,
  loadConfig,
  saveConfig,
  type DesktopConfig,
  type DesktopMode,
  type ServerProfile,
} from './profiles'

/** The config file lives beside window-state.json, in userData. */
function configFile(): string {
  return path.join(app.getPath('userData'), 'desktop-remote.json')
}

let cache: DesktopConfig | null = null
let refused: string | undefined

/** Read-through config accessor. The file is read once per process run. */
export function getConfig(): DesktopConfig {
  if (!cache) {
    const loaded = loadConfig(configFile())
    cache = loaded.config
    refused = loaded.refused
  }
  return cache
}

/** Non-empty when the on-disk config was written by a newer build and refused. */
export function refusedReason(): string | undefined {
  getConfig()
  return refused
}

function persist(next: DesktopConfig): DesktopConfig {
  saveConfig(configFile(), next)
  cache = next
  return next
}

/** The mode the app should boot in, and the profile it names (if any). */
export function startupMode(): { mode: DesktopMode; profile: ServerProfile | null; forcedLocal?: string } {
  const config = getConfig()
  if (config.mode === 'remote') {
    // Task 1.5: a runtime that predates WebContentsView must not load the
    // remote module. The legacy Electron 22 (Win7) line therefore falls back to
    // local, which is exactly the app it shipped as -- never a broken shell.
    const support = canHonourRemoteMode(process.versions.electron)
    if (!support.ok) {
      console.log(`[remote] remote mode refused on this runtime (${support.reason}); starting local`)
      return { mode: 'local', profile: null, forcedLocal: support.reason }
    }
  }
  return { mode: config.mode, profile: activeProfile(config) }
}

/** The projection the local shell renders. Never contains a secret. */
export function modeProjection() {
  const config = getConfig()
  const support = canHonourRemoteMode(process.versions.electron)
  return {
    mode: config.mode,
    profiles: config.profiles,
    activeProfileId: config.activeProfileId,
    refused: refused || '',
    containerSupported: support.ok,
    containerUnsupportedReason: support.ok ? '' : support.reason,
  }
}

/**
 * Probe a server without saving it.
 *
 * Probing is the only network call this module makes, and it goes through
 * ``probeServer`` -- which refuses a non-HTTPS origin before any request, so
 * there is no path here that could issue an insecure probe.
 */
export async function probe(rawOrigin: unknown): Promise<ProbeResult> {
  return probeServer(rawOrigin, fetch)
}

export function setMode(mode: unknown): { config: DesktopConfig; applied: boolean; reason: string } {
  const next = mode === 'remote' ? 'remote' : 'local'
  if (next === 'remote') {
    // Never persist a mode this runtime cannot honour (task 1.5).
    const support = canHonourRemoteMode(process.versions.electron)
    if (!support.ok) return { config: getConfig(), applied: false, reason: support.reason }
  }
  const config = getConfig()
  // Switching to local keeps the profiles (they are just candidates) but drops
  // the active pointer, so a later switch to remote must be explicit again.
  const updated: DesktopConfig = next === 'local'
    ? { ...config, mode: 'local', activeProfileId: null }
    : { ...config, mode: 'remote' }
  const saved = persist(updated)
  syncBrokerOrigin()
  return { config: saved, applied: true, reason: '' }
}

export function addServerProfile(rawOrigin: unknown, displayName?: unknown) {
  const config = getConfig()
  const result = addServer(config, rawOrigin, typeof displayName === 'string' ? displayName : undefined)
  if (!result.ok) return result
  persist(result.config)
  return { ok: true as const, id: result.id, existed: result.existed, config: modeProjection() }
}

export function removeServerProfile(id: unknown) {
  const config = getConfig()
  const target = typeof id === 'string' ? id : ''
  const profiles = config.profiles.filter((p) => p.id !== target)
  if (profiles.length === config.profiles.length) {
    return { ok: false as const, reason: 'unknown_profile' }
  }
  const activeProfileId = config.activeProfileId === target ? null : config.activeProfileId
  persist({ ...config, profiles, activeProfileId })
  return { ok: true as const, config: modeProjection() }
}

/**
 * Point the native broker at the server the config now names.
 *
 * In remote mode every credential and every business request the shell makes
 * belongs to the *active server*, so the broker's origin is derived from the
 * config here and nowhere else -- a renderer never passes an origin to a
 * credential-minting call. ``setBackendOrigin`` also tears down the previous
 * server's child partition, which is what makes switching servers discard the
 * old scope rather than carry a Cookie across.
 *
 * In local mode the bundled backend owns the transport, so its own origin is
 * restored rather than cleared. Clearing it was indistinguishable from "this
 * install has no backend": adding a server to the list -- which is not a mode
 * change -- blanked the origin the running backend had announced, and the
 * signed-in window had no transport until the app was restarted.
 */
export function syncBrokerOrigin(): void {
  const config = getConfig()
  if (config.mode !== 'remote') {
    // The bundled backend owns the transport, so restore *its* origin. Clearing
    // it here was indistinguishable from "this install has no backend".
    setBackendOrigin(getLocalBackendOrigin())
    return
  }
  // Remote mode: only the active server. When the config names none, the origin
  // is cleared -- falling back to the bundled backend would aim remote-mode
  // traffic at a different server than the config names.
  const profile = activeProfile(config)
  setBackendOrigin(profile ? profile.origin : '')
}

export function setActiveServer(id: unknown) {
  const config = getConfig()
  const target = typeof id === 'string' && id ? id : null
  if (target && !config.profiles.some((p) => p.id === target)) {
    return { ok: false as const, reason: 'unknown_profile' }
  }
  persist({ ...config, activeProfileId: target })
  syncBrokerOrigin()
  return { ok: true as const, config: modeProjection() }
}

/** Register the local shell's remote-configuration channels. */
export function setupRemoteConfigIPC(getWindow: () => BrowserWindow | null): void {
  const guard = (event: Electron.IpcMainInvokeEvent): void => {
    if (!isTrustedWindowFrame(event, getWindow())) {
      throw new Error('untrusted sender')
    }
  }
  const wrap = <T>(event: Electron.IpcMainInvokeEvent, fn: () => T) => {
    guard(event)
    try {
      return { ok: true, ...(fn() as object) }
    } catch (e) {
      return { ok: false, code: 'unexpected_error', message: 'the request could not be completed' }
    }
  }

  ipcMain.handle('desktop-mode-get', (event) => wrap(event, () => modeProjection()))

  ipcMain.handle('desktop-remote-probe', async (event, origin: string) => {
    guard(event)
    try {
      const result = await probe(origin)
      return result.ok ? { ok: true, meta: result.meta } : { ok: false, failure: result.failure }
    } catch {
      return { ok: false, failure: { kind: 'network', code: 'network_unreachable', message: 'the server could not be reached' } }
    }
  })

  ipcMain.handle('desktop-remote-add-server', (event, payload: { origin?: string; displayName?: string }) =>
    wrap(event, () => addServerProfile(payload?.origin, payload?.displayName)))

  ipcMain.handle('desktop-remote-remove-server', (event, id: string) =>
    wrap(event, () => removeServerProfile(id)))

  ipcMain.handle('desktop-remote-set-active', (event, id: string | null) =>
    wrap(event, () => setActiveServer(id)))

  ipcMain.handle('desktop-remote-set-mode', (event, mode: string) =>
    wrap(event, () => {
      const result = setMode(mode)
      return { config: modeProjection(), applied: result.applied, reason: result.reason }
    }))
}

/** Exported for tests / the startup log line. */
export { defaultConfig }
