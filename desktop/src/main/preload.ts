import type {
  BrokerAssetReply,
  BrokerLogoutReply,
  BrokerProbe,
  BrokerRequestReply,
  BrokerSessionReply,
  BrokerStatusReply,
  BrokerVoidReply,
} from './broker-protocol'
import * as electron from 'electron'

const { contextBridge, ipcRenderer } = electron

// webUtils.getPathForFile was added in Electron 32. The Win7 legacy build pins
// Electron to 22, whose typings don't declare webUtils, so we look it up off
// the runtime module instead of a static named import to keep tsc happy there.
const webUtils = (electron as { webUtils?: { getPathForFile(file: File): string } }).webUtils

contextBridge.exposeInMainWorld('electronAPI', {
  getBackendPort: () => ipcRenderer.invoke('get-backend-port'),
  getBackendStatus: () => ipcRenderer.invoke('get-backend-status'),
  getBackendError: () => ipcRenderer.invoke('get-backend-error'),
  getDataDir: () => ipcRenderer.invoke('get-data-dir') as Promise<string>,
  // Real on-disk path of a File picked or dropped by the user, so the local
  // backend can read it directly instead of receiving the bytes over HTTP.
  // '' for files with no path (e.g. a pasted clipboard image).
  getPathForFile: (file: File): string => {
    try {
      return webUtils?.getPathForFile(file) || ''
    } catch {
      return ''
    }
  },
  restartBackend: () => ipcRenderer.invoke('restart-backend'),
  selectDirectory: () => ipcRenderer.invoke('select-directory'),
  selectFile: (filters?: Electron.FileFilter[]) => ipcRenderer.invoke('select-file', filters),
  openPath: (targetPath: string) => ipcRenderer.invoke('open-path', targetPath) as Promise<string>,

  // Each listener registrar returns an unsubscribe fn so renderers can clean
  // up on unmount / effect re-run and avoid accumulating duplicate handlers.
  onBackendStatus: (
    callback: (data: { status: string; port?: number; error?: string; code?: string; path?: string }) => void,
  ) => {
    const handler = (
      _event: unknown,
      data: { status: string; port?: number; error?: string; code?: string; path?: string },
    ) => callback(data)
    ipcRenderer.on('backend-status', handler)
    return () => ipcRenderer.removeListener('backend-status', handler)
  },

  onBackendLog: (callback: (line: string) => void) => {
    const handler = (_event: unknown, line: string) => callback(line)
    ipcRenderer.on('backend-log', handler)
    return () => ipcRenderer.removeListener('backend-log', handler)
  },

  // Window controls (custom titlebar on Windows)
  windowMinimize: () => ipcRenderer.invoke('window-minimize'),
  windowMaximize: () => ipcRenderer.invoke('window-maximize'),
  windowClose: () => ipcRenderer.invoke('window-close'),
  windowIsMaximized: () => ipcRenderer.invoke('window-is-maximized'),
  onMaximizeChange: (callback: (maximized: boolean) => void) => {
    const handler = (_event: unknown, max: boolean) => callback(max)
    ipcRenderer.on('window-maximize-changed', handler)
    return () => ipcRenderer.removeListener('window-maximize-changed', handler)
  },

  // App menu / shortcut actions forwarded from the main process.
  onMenuAction: (callback: (action: string) => void) => {
    const handler = (_event: unknown, action: string) => callback(action)
    ipcRenderer.on('menu-action', handler)
    return () => ipcRenderer.removeListener('menu-action', handler)
  },

  // Current app version (e.g. "0.0.5"), shown in the NavRail footer.
  getAppVersion: () => ipcRenderer.invoke('get-app-version'),

  // Ask for OS-level microphone access (prompts once on macOS; true elsewhere).
  requestMicAccess: () => ipcRenderer.invoke('mic-request-access') as Promise<boolean>,

  // Launch-at-login toggle (macOS + Windows). get returns the effective state;
  // set returns the real outcome so the UI can surface refusals/errors.
  getLoginItemEnabled: () => ipcRenderer.invoke('get-login-item') as Promise<boolean>,
  setLoginItemEnabled: (enabled: boolean) =>
    ipcRenderer.invoke('set-login-item', enabled) as Promise<{
      ok: boolean
      enabled: boolean
      error: string
    }>,

  // Themes (bundled + user themes from ~/.cow/themes), assets inlined.
  listThemes: () => ipcRenderer.invoke('themes-list') as Promise<Record<string, unknown>[]>,
  getThemesDir: () => ipcRenderer.invoke('themes-dir') as Promise<string>,
  // Optional app config (first-run default theme + display name). Null in
  // the standard build.
  getAppConfig: () =>
    ipcRenderer.invoke('app-config-get') as Promise<{ defaultTheme?: string; appName?: string } | null>,

  // Generic HTTPS relay via the main process (bypasses the renderer's CORS
  // restrictions for external endpoints). Optional extensions may use it.
  //
  // It is a foreign-endpoint relay only: the main process refuses any URL that
  // points at the backend, the native auth endpoints or the loopback callback
  // (design D8), so it cannot be turned into a bypass around the broker.
  httpRelay: (req: {
    url: string
    method?: string
    headers?: Record<string, string>
    body?: string
  }) =>
    ipcRenderer.invoke('http-relay', req) as Promise<{
      ok: boolean
      status: number
      headers: Record<string, string>
      body: string
    }>,

  // ---- Desktop identity broker (design D8) -------------------------------
  //
  // The session Bearer lives in the main process and is not reachable from
  // here: there is no channel that returns a token, and no channel that accepts
  // a header, an origin or an Authorization value. The renderer sees only the
  // desensitized projection, a single business-request transport and an opaque
  // asset URL for the transports that cannot carry a header (EventSource,
  // <img>, download).
  desktopAuthProbe: () =>
    ipcRenderer.invoke('desktop-auth-probe') as Promise<BrokerProbe>,
  desktopAuthStatus: () => ipcRenderer.invoke('desktop-auth-status') as Promise<BrokerStatusReply>,
  desktopAuthBegin: () => ipcRenderer.invoke('desktop-auth-begin') as Promise<BrokerSessionReply>,
  desktopAuthCancel: () => ipcRenderer.invoke('desktop-auth-cancel') as Promise<{ ok: boolean }>,
  desktopAuthLogout: () => ipcRenderer.invoke('desktop-auth-logout') as Promise<BrokerLogoutReply>,
  desktopAuthRefresh: () => ipcRenderer.invoke('desktop-auth-refresh') as Promise<BrokerSessionReply>,
  desktopTenantSelect: (tenantId: string) =>
    ipcRenderer.invoke('desktop-tenant-select', tenantId) as Promise<BrokerSessionReply>,
  desktopPasswordChange: (payload: { oldPassword: string; newPassword: string }) =>
    ipcRenderer.invoke('desktop-password-change', payload) as Promise<BrokerVoidReply>,
  desktopRequest: (req: { path: string; method?: string; body?: string }) =>
    ipcRenderer.invoke('desktop-request', req) as Promise<BrokerRequestReply>,
  desktopUpload: (req: {
    path: string
    method?: string
    form: { fields: Array<{ name: string; value: string }>; files: Array<{ name: string; filename: string; contentType: string; bytes: ArrayBuffer }> }
  }) => ipcRenderer.invoke('desktop-upload', req) as Promise<BrokerRequestReply>,
  desktopAssetUrl: (req: { path: string; kind?: 'get' | 'stream' }) =>
    ipcRenderer.invoke('desktop-asset-url', req) as Promise<BrokerAssetReply>,
  desktopAssetUrlSync: (req: { path: string; kind?: 'get' | 'stream' }) =>
    ipcRenderer.sendSync('desktop-asset-url-sync', req) as BrokerAssetReply,

  // Auto-update: trigger checks/download/install and subscribe to status. The
  // optional lang routes installer downloads to the China CDN mirror (zh) or R2.
  checkForUpdate: (lang?: string) => ipcRenderer.invoke('update-check', lang),
  downloadUpdate: (lang?: string) => ipcRenderer.invoke('update-download', lang),
  installUpdate: () => ipcRenderer.invoke('update-install'),
  // Extra query parameters for the update feed URL; pass {} to clear.
  setUpdateFeedQuery: (params: Record<string, string>) => ipcRenderer.invoke('update-feed-query', params),
  onUpdateStatus: (callback: (status: unknown) => void) => {
    const handler = (_event: unknown, status: unknown) => callback(status)
    ipcRenderer.on('update-status', handler)
    return () => ipcRenderer.removeListener('update-status', handler)
  },

  setAppIcon: (iconUrl: string, icoUrl?: string) =>
    ipcRenderer.invoke('set-app-icon', iconUrl, icoUrl) as Promise<boolean>,
  setAppTitle: (title: string) => ipcRenderer.invoke('set-app-title', title) as Promise<boolean>,

  // Show a native OS notification; clicking it focuses the window and asks the
  // renderer (via onOpenSession) to open the given session.
  notify: (payload: { title?: string; body?: string; sessionId?: string; silent?: boolean; force?: boolean }) =>
    ipcRenderer.invoke('notify', payload) as Promise<boolean>,
  onOpenSession: (callback: (sessionId: string) => void) => {
    const handler = (_event: unknown, sessionId: string) => callback(sessionId)
    ipcRenderer.on('open-session', handler)
    return () => ipcRenderer.removeListener('open-session', handler)
  },

  // ---- Remote-mode configuration (change add-desktop-remote-web-workbench) --
  //
  // The local shell's own half of remote mode: choose/probe a server, switch
  // mode. It is a *different* API from ``window.desktopHost``, which the remote
  // Web container gets and which exposes no server management at all.
  desktopModeGet: () =>
    ipcRenderer.invoke('desktop-mode-get') as Promise<{
      ok: boolean
      mode: 'local' | 'remote'
      profiles: Array<{ id: string; origin: string; displayName: string }>
      activeProfileId: string | null
      serverOrigin: string
      refused: string
      containerSupported: boolean
      containerUnsupportedReason: string
    }>,
  desktopLoginServerSet: (origin: string) => ipcRenderer.invoke('desktop-login-server-set', origin),
  desktopRemoteProbe: (origin: string) => ipcRenderer.invoke('desktop-remote-probe', origin),
  desktopRemoteAddServer: (payload: { origin: string; displayName?: string }) =>
    ipcRenderer.invoke('desktop-remote-add-server', payload),
  desktopRemoteRemoveServer: (id: string) => ipcRenderer.invoke('desktop-remote-remove-server', id),
  desktopRemoteSetActive: (id: string | null) => ipcRenderer.invoke('desktop-remote-set-active', id),
  desktopRemoteSetMode: (mode: 'local' | 'remote') => ipcRenderer.invoke('desktop-remote-set-mode', mode),
  // Attach/detach the remote container (tasks 4.1/4.6). The remote page never
  // gets these two channels: only the local shell can ask for them.
  desktopRemoteConnect: () => ipcRenderer.invoke('remote-container-start') as Promise<{
    ok: boolean; code?: string; message?: string
  }>,
  desktopRemoteDisconnect: () => ipcRenderer.invoke('remote-container-stop') as Promise<{
    ok: boolean; revoked: boolean; message: string
  }>,
  /**
   * The host's sign-out feed (change ``fix-desktop-relogin-session-sync``,
   * task 2.2). The container asked to end the account; the local shell has to
   * refresh its login/connection entry even though the document that asked is
   * already gone. The payload is a phase and an outcome -- no account, no token.
   */
  onDesktopLogoutState: (callback: (notice: {
    phase: 'started' | 'finished'
    ok: boolean
    revoked: boolean
    code: string
    message: string
  }) => void) => {
    const handler = (_event: unknown, notice: {
      phase: 'started' | 'finished'
      ok: boolean
      revoked: boolean
      code: string
      message: string
    }) => callback(notice)
    ipcRenderer.on('desktop:logout-state', handler)
    return () => ipcRenderer.removeListener('desktop:logout-state', handler)
  },

  platform: process.platform,
  // OS UI language (e.g. "zh-CN"), read synchronously so the renderer can pick
  systemLocale: (() => {
    try {
      return ipcRenderer.sendSync('get-system-locale') as string
    } catch {
      return ''
    }
  })(),
})
