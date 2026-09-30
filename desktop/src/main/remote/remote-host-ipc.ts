// The main-process side of the remote bridge (task 4.3).
//
// One place decides whether a bridge call may act natively, and one place
// performs it. The decision is ``host-bridge.checkSender`` (frame identity,
// document generation, origin, not-a-content-surface) plus
// ``checkBridgeCall`` (method allow-list and parameter shape); nothing here
// re-invents either, so a new method cannot be added without also being
// validated there.
//
// The channels are registered only in remote mode, and only for the container's
// webContents: a local React renderer that happens to send the same channel name
// fails the first check (its webContents is not the container's).

import { app, BrowserWindow, dialog, ipcMain, shell, type IpcMainInvokeEvent, type WebContents } from 'electron'
import { writeFile } from 'fs/promises'
import * as path from 'path'
import {
  BRIDGE_VERSION, checkBridgeCall,
  checkOpenExternal, checkSender, routeOpen,
  type SenderContext, type SenderDescription,
} from './host-bridge'
import {
  applyChooseWorkspace,
  applyDisconnectWorkspace,
  bridgeCapabilitiesPayload,
  isRemoteLocalFilesEnabled,
  PICKER_MESSAGE,
  saveAsApprovalApplicability,
  refuseAutomaticWriteBack,
} from './local-files-bridge'
import { loadCandidates, saveCandidates } from '../local-files/candidates'
import { remoteGrantRegistry } from './local-files-bridge'
import { confirmLocalContext, type BindingTransport } from './binding-setup'
import type { GrantScope } from '../local-files/grants'

export const CHANNEL_HELLO = 'desktop:bridge:hello'
export const CHANNEL_CALL = 'desktop:bridge:call'
export const CHANNEL_EVENT = 'desktop:bridge:event'

/** What the main process knows about the live container. */
export interface RemoteHostRegistration {
  webContents: WebContents
  /**
   * Read at call time, never captured: the registered frame ids only exist
   * after the first navigation commits, and the generation changes on every
   * later one. A snapshot would refuse the shell that is legitimately running.
   */
  context: () => SenderContext
  /** Sends one host event to the shell document. */
  emit: (payload: unknown) => void
  /**
   * Shows a same-origin server document or attachment (``routeOpen`` decided
   * it). Optional so a caller that only wires the bridge cannot silently get a
   * native window: without it, ``openExternal`` refuses the content target.
   */
  openContent?: (url: string, kind: 'document' | 'attachment') => void
  /**
   * The credentialed transports ``bindContext`` needs (native bearer for the
   * device/workspace/grant calls, paired Web cookie for the binding itself).
   * Optional: a host that cannot reach the server refuses the confirmation
   * rather than inventing a binding id.
   */
  bindingTransport?: BindingTransport
  /**
   * The local read path, once a directory has been confirmed (change
   * ``fix-desktop-local-context-and-tool-calls``, task 2.4).
   *
   * Optional so a host that only wires the bridge keeps working; without it the
   * confirmation still succeeds, but this machine never becomes reachable and a
   * read would time out -- which is exactly the failure this closes.
   */
  localFiles?: {
    /** A workspace became real: start serving it. */
    bound: (bound: {
      origin: string
      deviceId: string
      workspaceId: string
      grantId: string
    }) => void
    /** The page disconnected: `all` is true when every scope was dropped. */
    revoked: (all: boolean) => void
  }
}

let registration: RemoteHostRegistration | null = null

/** Refuses every credentialed call: used when no transport was injected. */
const unavailableTransport: BindingTransport = {
  native: async () => ({ status: 503, body: '{"code":"feature_unavailable","message":"the local directory cannot be confirmed"}' }),
  web: async () => ({ status: 503, body: '{"code":"feature_unavailable","message":"the local directory cannot be confirmed"}' }),
}

/** The device-row platform the server accepts, derived from the host. */
function clientPlatform(): string {
  if (process.platform === 'darwin') return 'macos'
  if (process.platform === 'win32') return 'windows'
  return 'linux'
}

/** The application version this host reports for its own device row. */
function clientVersion(): string {
  try {
    return app.getVersion() || '0.0.0'
  } catch {
    return '0.0.0'
  }
}

/** Describe the caller, using the frame Electron resolved -- never the payload. */
export function describeSender(event: IpcMainInvokeEvent): SenderDescription {
  const contents = event.sender
  const frame = event.senderFrame
  const mainFrame = contents.mainFrame
  return {
    webContentsId: contents.id,
    frameProcessId: frame ? frame.processId : -1,
    frameRoutingId: frame ? frame.routingId : -1,
    isMainFrame: !!frame && !!mainFrame && frame.processId === mainFrame.processId
      && frame.routingId === mainFrame.routingId,
    hasParentFrame: !!(frame && frame.parent),
    url: frame ? frame.url : '',
    generation: 0,
  }
}

function refusal(code: string, message: string): { ok: false; code: string; message: string } {
  return { ok: false, code, message }
}

/** Register the container as *the* bridge caller. */
export function registerRemoteHost(entry: RemoteHostRegistration | null): void {
  registration = entry
}

/** The generation a *live* document learns; a stale one is refused first. */
function handshake(event: IpcMainInvokeEvent): { ok: boolean; generation?: number; code?: string; message?: string } {
  const current = registration
  if (!current) return { ok: false, code: 'feature_unavailable', message: 'the remote container is not running' }
  const context = current.context()
  const sender = describeSender(event)
  const verdict = checkSender({ ...sender, generation: context.generation }, context)
  if (!verdict.ok) return { ok: false, code: verdict.code, message: verdict.message }
  return { ok: true, generation: context.generation }
}

/**
 * Run one validated call.
 *
 * ``generation`` arrives from the document: it must equal the registered
 * generation, so a document that was replaced by a navigation cannot act as the
 * shell even though it still exists in the process.
 */
async function dispatch(
  event: IpcMainInvokeEvent,
  call: { method?: string; params?: Record<string, unknown>; generation?: number },
): Promise<{ ok: boolean; code?: string; message?: string; data?: unknown }> {
  const current = registration
  if (!current) return refusal('feature_unavailable', 'the remote container is not running')
  const context = current.context()
  const sender = describeSender(event)
  const verdict = checkSender(
    { ...sender, generation: typeof call?.generation === 'number' ? call.generation : -1 },
    context,
  )
  if (!verdict.ok) return verdict
  const shape = checkBridgeCall({ method: String(call?.method || ''), params: call?.params })
  if (!shape.ok) return shape
  const params = (call?.params || {}) as Record<string, unknown>
  switch (call?.method) {
    case 'getCapabilities':
      // Phase-1 callers only see phase-1 methods until local-files opens;
      // the full allow-list is still validated by checkBridgeCall.
      return {
        ok: true,
        data: bridgeCapabilitiesPayload({
          bridge: BRIDGE_VERSION,
          generation: context.generation,
          saveAsApproval: saveAsApprovalApplicability(),
        }),
      }
    case 'suspendLocalContext':
      // In remote mode there is no local import context to suspend; the answer
      // is an acknowledgement, never a local file operation.
      applyDisconnectWorkspace()
      return { ok: true, data: { suspended: true } }
    case 'saveArtifact': {
      const name = String(params.name)
      const content = String(params.content)
      const choice = await dialog.showSaveDialog({ defaultPath: name })
      if (choice.canceled || !choice.filePath) {
        return { ok: false, code: 'invalid_request', message: 'the save was cancelled' }
      }
      await writeFile(choice.filePath, Buffer.from(content, 'base64'))
      // The chosen path stays in the main process: the page learns only that
      // something was written, never where. Applicability is recorded so an
      // auditor sees why no approval ticket was opened (task 12.3 / F17).
      return {
        ok: true,
        data: { saved: true, approval: saveAsApprovalApplicability() },
      }
    }
    case 'openExternal': {
      const url = String(params.url)
      const allowed = checkOpenExternal(url, true)
      if (!allowed.ok) return allowed
      // The page asked to *open* a URL; where it actually goes is the host's
      // decision (`routeOpen`), not the page's. A foreign link becomes a browser
      // tab; a same-origin preview or attachment becomes the isolated content
      // window, which has no bridge -- this is how `window.open(preview_url)`
      // keeps working in a container without any page-side branch.
      const route = routeOpen(url, { origin: context.origin, entryPaths: context.entryPaths })
      if (!route.ok) return route
      if (route.route === 'content-window') {
        if (!current.openContent) {
          return refusal('feature_unavailable', 'this container cannot show content documents')
        }
        current.openContent(route.url, route.kind)
        return { ok: true, data: { opened: true, via: 'content-window' } }
      }
      await shell.openExternal(route.url)
      return { ok: true, data: { opened: true } }
    }
    case 'onHostEvent':
      // Subscription is a renderer-side concept; the preload registers a
      // listener for the event channel and there is nothing to run here.
      return { ok: true, data: { subscribed: false } }
    case 'chooseWorkspace': {
      if (!isRemoteLocalFilesEnabled()) {
        return refusal('feature_unavailable', 'desktop local files are not available')
      }
      const scope = (params.scope || {}) as GrantScope
      const parent = BrowserWindow.fromWebContents(event.sender)
      const dialogOpts: Electron.OpenDialogOptions = {
        properties: ['openDirectory'],
        message: PICKER_MESSAGE,
      }
      const dialogResult = parent
        ? await dialog.showOpenDialog(parent, dialogOpts)
        : await dialog.showOpenDialog(dialogOpts)
      const candidatesFile = path.join(app.getPath('userData'), 'desktop-directory-candidates.json')
      const candidates = loadCandidates(candidatesFile)
      const result = applyChooseWorkspace({ scope, dialogResult, candidates })
      if (!result.ok) {
        return { ok: false, code: result.code, message: result.message }
      }
      if (result.activated) {
        saveCandidates(candidatesFile, candidates)
        return { ok: true, data: { activated: true, grant: result.grant } }
      }
      return { ok: true, data: { activated: false, reason: result.reason } }
    }
    case 'disconnectWorkspace': {
      const scope = (params.scope || {}) as Record<string, string>
      const result = applyDisconnectWorkspace(scope)
      const all = !scope.serverId && !scope.userId && !scope.tenantId && !scope.deviceId
      // The local read path is told as well: a disconnected grant must stop
      // being servable *now*, not at the next command's re-check, and with
      // nothing left bound the device connection has no reason to stay up.
      try {
        registration?.localFiles?.revoked(all)
      } catch {
        /* the host may already be tearing down */
      }
      return { ok: true, data: result }
    }
    case 'bindContext': {
      if (!isRemoteLocalFilesEnabled()) {
        return refusal('feature_unavailable', 'desktop local files are not available')
      }
      const scope = (params.scope || {}) as GrantScope
      const grant = remoteGrantRegistry.get(scope)
      const result = await confirmLocalContext(
        {
          scope,
          installationId: String(params.installationId),
          label: String(params.label),
          grantVersion: grant ? grant.grantVersion : 0,
          agentId: String(params.agentId),
          businessSessionId: String(params.businessSessionId),
          contextNonce: String(params.contextNonce),
          generation: context.generation,
          // Host facts, never the page's: a page cannot claim a platform or a
          // version for the device row it causes to be written.
          platform: clientPlatform(),
          clientVersion: clientVersion(),
        },
        context.generation,
        grant,
        current.bindingTransport ?? unavailableTransport,
      )
      if (!result.ok) {
        return { ok: false, code: result.code, message: result.message }
      }
      if (!result.workspaceId) {
        // A binding without a server workspace is the half-confirmed state the
        // confirmation path exists to prevent: report it instead of letting the
        // page publish a directory the tools cannot read.
        return refusal('invalid_request', 'the local directory was not registered')
      }
      // The directory is real now: let the host start the device connection so
      // a command for this workspace has somewhere to run. A failure here does
      // not undo the binding, but it is worth reporting rather than silently
      // leaving a directory the tools cannot read (task 2.4).
      if (registration?.localFiles && grant && result.deviceId) {
        try {
          registration.localFiles.bound({
            origin: context.origin,
            deviceId: result.deviceId,
            workspaceId: result.workspaceId,
            grantId: grant.id,
          })
        } catch (err) {
          return refusal('feature_unavailable',
            `the local directory was registered but this machine cannot serve it: ${String(err)}`)
        }
      }
      return {
        ok: true,
        data: {
          bindingId: result.bindingId,
          workspaceId: result.workspaceId,
          grantVersion: result.grantVersion ?? 0,
          generation: result.generation,
        },
      }
    }
    case 'writeBack':
    case 'runLocalScript':
      return refuseAutomaticWriteBack()
    default:
      return refusal('feature_unavailable', 'the bridge method is not part of this bridge version')
  }
}

/** Register the two bridge channels. Idempotent, so a mode switch is safe. */
export function setupRemoteHostIPC(): void {
  ipcMain.removeHandler(CHANNEL_HELLO)
  ipcMain.removeHandler(CHANNEL_CALL)
  ipcMain.handle(CHANNEL_HELLO, (event) => handshake(event))
  ipcMain.handle(CHANNEL_CALL, (event, call) => dispatch(event, call || {}))
}

/** Stop answering the bridge and send one final suspend notice. */
export function teardownRemoteHostIPC(notify?: () => void): void {
  ipcMain.removeHandler(CHANNEL_HELLO)
  ipcMain.removeHandler(CHANNEL_CALL)
  const current = registration
  registration = null
  if (notify) notify()
  try {
    current?.webContents.send(CHANNEL_EVENT, { type: 'suspended' })
  } catch {
    /* the container is already gone */
  }
}
