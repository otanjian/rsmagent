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
  checkOpenExternal, checkSender, isSessionInvalidCode, routeOpen,
  type SenderContext, type SenderDescription,
} from './host-bridge'
import { status } from '../auth-broker'
import type { NativeAction } from '../project-browser/native-actions'
import { NATIVE_ACTION_BY_METHOD } from '../project-browser/native-actions'
import {
  activateGrant,
  applyDisconnectWorkspace,
  bridgeCapabilitiesPayload,
  chooseWorkspaceMessage,
  isRemoteLocalFilesEnabled,
  normalizeGrantPurpose,
  saveAsApprovalApplicability,
  refuseAutomaticWriteBack,
} from './local-files-bridge'
import { loadCandidates, saveCandidates } from '../local-files/candidates'
import { DirectorySelectionService } from '../local-files/selection'
import { installationIdentityFor } from '../installation-identity'
import { remoteGrantRegistry } from './local-files-bridge'
import { confirmLocalContext, type BindingTransport } from './binding-setup'
import type { GrantScope } from '../local-files/grants'
import { readSapPage, type ReadContext, type ReadRequest } from './sap-page-read'

/**
 * One selection state machine for the process. State is keyed by scope inside it,
 * so sharing the instance across senders cannot leak one scope's selection into
 * another's; the per-scope keying is what makes that safe.
 */
const directorySelection = new DirectorySelectionService({ messageFor: chooseWorkspaceMessage })

export const CHANNEL_HELLO = 'desktop:bridge:hello'
export const CHANNEL_CALL = 'desktop:bridge:call'
export const CHANNEL_EVENT = 'desktop:bridge:event'

/** What the main process knows about the live container. */
export interface RemoteHostRegistration {
  webContents: WebContents
  sapReadContext?: (request: ReadRequest) => Promise<ReadContext>
  sapLayout?: (params: Record<string, unknown>) => object
  sapLogin?: (binding: string, action: string, tenant: string) => Promise<object>
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
   * End the account through the trusted host (change
   * ``fix-desktop-relogin-session-sync``, task 1.2/2.1).
   *
   * This is the *only* account action the container may start, and it is the
   * existing detach path: block the container, revoke the native session, tell
   * the local shell. Optional so a host that never wired it refuses the method
   * by name rather than pretending the account ended. The reply is best-effort
   * -- the host usually destroys this document before it settles, which is why
   * the local shell, not the page, is told the final state.
   */
  signOut?: () => Promise<{ ok: boolean; revoked: boolean; message: string }>
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
      /**
       * The server's binding row, the selection generation in effect and the
       * grant version -- everything a locally-issued edit frame must *name*
       * (task 9.2). They are handed over here, at the one moment all of them are
       * known together, rather than re-derived later from a page's memory of the
       * confirmation.
       */
      bindingId: string
      selectionGeneration: number
      grantVersion: number
      /** The user's own label. Display only: the absolute path never travels. */
      label: string
      /** True only for a `project-execution` grant; a read grant is not widened. */
      executable: boolean
      /**
       * The Agent and the chat this confirmation belongs to (task 9.3).
       *
       * Recorded so a *later document* -- the same page after a reload -- can ask
       * for this project and be answered about its own chat rather than about
       * whatever the previous document left behind. Both are the values the
       * server verified the binding with, not a page's later claim.
       */
      agentId: string
      businessSessionId: string
    }) => void
    /** The page disconnected: `all` is true when every scope was dropped. */
    revoked: (all: boolean) => void
    /**
     * The live local context for one chat, or why there is none (task 9.3).
     *
     * Read *now*: the recorded confirmation is matched against the live grant
     * registry and the device connection on every call, so a revoked or moved
     * project answers ``stale`` instead of being handed back as if it were open.
     * Optional, so a host that never wired it refuses the method by name.
     */
    localContext?: (request: { agentId: string; businessSessionId: string }) => unknown
  }
  /**
   * The local project **source** the file panel reads and edits (task 9.2).
   *
   * The page cannot be trusted with a directory, and the server cannot resolve a
   * path that exists only on this machine, so the browser lives here in the main
   * process and the page only ever names a workspace. Optional: a host that does
   * not wire it refuses these methods by name rather than falling back to the
   * backend's own working directory, which would answer about a different file.
   */
  projects?: {
    describe: (workspaceId: string) => Promise<unknown> | unknown
    tree: (params: Record<string, unknown>) => Promise<unknown> | unknown
    search: (params: Record<string, unknown>) => Promise<unknown> | unknown
    resolve: (params: Record<string, unknown>) => Promise<unknown> | unknown
    read: (params: Record<string, unknown>) => Promise<unknown> | unknown
    write: (params: Record<string, unknown>) => Promise<unknown> | unknown
    /**
     * The system actions on a project file (task 9.4).
     *
     * One entry, not four: the host verifies the file and the grant once, then
     * acts -- and whether the *action* was one this build publishes is already
     * decided by ``checkBridgeCall`` before the port is reached, so a second
     * allow-list here would be a list that can disagree with the first.
     */
    nativeAction?: (
      action: NativeAction,
      params: Record<string, unknown>,
    ) => Promise<unknown> | unknown
    /**
     * The isolated preview of a project file (task 9.5).
     *
     * Answers with a refusal, or with a short-lived protected URL the page may
     * *embed* and not read: the bytes stay in the main process until the frame
     * asks for them, and the answer carries no directory.
     */
    previewFile?: (params: Record<string, unknown>) => Promise<unknown> | unknown
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

/**
 * The desktop login is gone and signing in again is the only recovery.
 *
 * ``auth_required`` is the stable code the console maps to its desktop re-login
 * entry (task 2.4). It is deliberately used *only* when the session is actually
 * missing -- never for a tool permission, a tenant or a device problem, which
 * keep their own codes so the page does not offer a useless re-login.
 */
function signInRequired(): { ok: false; code: string; message: string } {
  return refusal('auth_required', 'the desktop login is no longer valid; sign in again')
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
        data: { ...bridgeCapabilitiesPayload({
          bridge: BRIDGE_VERSION,
          generation: context.generation,
          saveAsApproval: saveAsApprovalApplicability(),
        }), sapPageRead: process.platform === 'darwin' && Boolean(current.sapReadContext),
          sapLoginMemory: process.platform === 'darwin' && Boolean(current.sapLogin) },
      }
    case 'sapWorkbenchLayout':
      return current.sapLayout ? {ok: true, data: current.sapLayout(params)} : refusal('feature_unavailable', 'Layout preferences are unavailable')
    case 'manageSapLogin': {
      if (!current.sapLogin || process.platform !== 'darwin') return refusal('sap_login_unavailable', 'SAP login memory is unavailable')
      const broker = status()
      if (!broker.session || broker.blockedReason) return signInRequired()
      try {
        const data = await current.sapLogin(String(params.binding_id), String(params.action), String(params.tenant_id))
        if (registration !== current || current.context().generation !== context.generation || status().session?.userId !== broker.session.userId) return refusal('stale_context', 'the workbench changed')
        return {ok:true, data}
      } catch (error) {
        const code = (error as {code?: string}).code
        return refusal(code === 'sap_keychain_unavailable' ? code : 'sap_login_unavailable', 'SAP login memory is unavailable')
      }
    }
    case 'readSapPage': {
      if (!current.sapReadContext || process.platform !== 'darwin') return refusal('page_read_unsupported', 'macOS page reading is unavailable')
      const broker = status()
      if (!broker.session || broker.blockedReason) return signInRequired()
      const request = params as ReadRequest
      try {
        const data = await readSapPage(current.webContents, request, () => current.sapReadContext!(request))
        if (registration !== current || current.context().generation !== context.generation || status().session?.epoch !== broker.session.epoch) {
          return refusal('page_changed', 'the workbench changed during reading')
        }
        return {ok: true, data}
      } catch (error) {
        const code = (error as {code?: string}).code
        return refusal(code && ['login_required', 'page_changed', 'page_read_unsupported', 'page_read_unavailable', 'result_too_large'].includes(code) ? code : 'extract_failed', 'SAP page reading did not complete')
      }
    }
    case 'signOut': {
      // The one account action the container owns. It runs the host's existing
      // detach/logout (single-flighted there, so a second call merges rather
      // than racing), and the host tells the local shell the outcome -- this
      // document is normally destroyed before the reply settles, so the page
      // must not be the authority on the final state.
      const port = current.signOut
      if (!port) {
        return refusal('feature_unavailable', 'this host does not end a desktop session')
      }
      try {
        const result = await port()
        return {
          ok: result.ok,
          code: result.ok ? undefined : 'logout_incomplete',
          message: result.message,
          data: { signedOut: result.ok, revoked: result.revoked },
        }
      } catch (err) {
        return refusal('logout_incomplete', String((err as Error)?.message || err))
      }
    }
    case 'suspendLocalContext': {
      const reason = String(params.reason || '')
      // A document that is merely going away is not an authorization change: a
      // reload replaces the document, and the local project the user opened has
      // to be *there* for the document that takes over -- which re-verifies
      // before it shows or does anything (task 9.3). An explicit suspend (a
      // logout, a tenant switch) is an authorization change, so the grants go
      // with it and the device connection is taken down.
      if (reason !== 'page-unload') {
        applyDisconnectWorkspace()
        try {
          registration?.localFiles?.revoked(true)
        } catch {
          /* the host may already be tearing down */
        }
      }
      return { ok: true, data: { suspended: true } }
    }
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
      // Refuse before the dialog opens when the native session is already known
      // to be missing or a sign-out is unconfirmed (task 2.4). Opening a picker
      // whose binding is certain to fail would be a dialog with no honest end;
      // the stable code sends the page to its re-login entry instead.
      const broker = status()
      if (!broker.session || broker.blockedReason) {
        // ``auth_required`` is the one code the page turns into its re-login
        // entry, and both states are that: no usable session. The message still
        // says which, so a support log is not ambiguous. A *permission* or
        // tenant refusal keeps its own code and never reaches this branch.
        return refusal('auth_required', broker.blockedReason
          ? `the previous sign-out is unfinished (${broker.blockedReason}); sign in again`
          : 'the desktop login is no longer valid; sign in again')
      }
      const scope = (params.scope || {}) as GrantScope
      // The purpose decides the authorization: a read-only reference and an
      // "open my project here" grant are different, so the dialog says which
      // one the user is about to make. Anything unrecognised is read-only.
      const purpose = normalizeGrantPurpose(params.purpose)
      const parent = BrowserWindow.fromWebContents(event.sender)
      const candidatesFile = path.join(app.getPath('userData'), 'desktop-directory-candidates.json')
      // Through the selection service (tasks 2.3/2.4) rather than straight from
      // the dialog: it owns the generation guard, the cancel semantics and the
      // reported phase, none of which a bare `await dialog` has. The service
      // returns a purpose-limited record and never a path.
      const candidates = loadCandidates(candidatesFile)
      const outcome = await directorySelection.select({
        scope,
        purpose,
        pick: (request) => {
          const dialogOpts: Electron.OpenDialogOptions = {
            properties: ['openDirectory'],
            message: request.message,
          }
          return parent
            ? dialog.showOpenDialog(parent, dialogOpts)
            : dialog.showOpenDialog(dialogOpts)
        },
        commit: ({ absolutePath, purpose: committedPurpose }) => {
          const activated = activateGrant({
            scope,
            absolutePath,
            purpose: committedPurpose,
            candidates,
          })
          if (!activated.ok) {
            const error = new Error(activated.message) as Error & { code?: string }
            error.code = activated.code
            throw error
          }
          return activated.grant
        },
        // The service activates before it prepares, so it needs a way to undo a
        // commit whose attempt was discarded. Without this it would have to
        // report the discarded grant as still in effect.
        release: (grant) => {
          remoteGrantRegistry.revoke(grant.id)
        },
      })
      if (!outcome.ok) {
        return { ok: false, code: outcome.code, message: outcome.message, data: { state: outcome.state } }
      }
      if (!outcome.committed) {
        return { ok: true, data: { activated: false, reason: outcome.reason, state: outcome.state } }
      }
      // Only a committed selection is worth persisting as a candidate.
      saveCandidates(candidatesFile, candidates)
      return {
        ok: true,
        data: {
          activated: true,
          grant: outcome.state.effective,
          state: outcome.state,
        },
      }
    }
    case 'disconnectWorkspace': {
      const scope = (params.scope || {}) as Record<string, string>
      const result = applyDisconnectWorkspace(scope)
      const all = !scope.serverId && !scope.userId && !scope.tenantId && !scope.deviceId
      // The selection state goes with it. A revoked grant must not keep reading as
      // "ready" -- `markRevoked` keeps the generation so an in-flight dialog is
      // still superseded rather than committing a root that was just disconnected.
      if (all) {
        directorySelection.forget({})
      } else {
        directorySelection.markRevoked(scope as unknown as GrantScope)
      }
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
    case 'localContext': {
      if (!isRemoteLocalFilesEnabled()) {
        return refusal('feature_unavailable', 'desktop local files are not available')
      }
      const port = current.localFiles
      if (!port?.localContext) {
        return refusal('feature_unavailable', 'this host keeps no local project for a chat')
      }
      try {
        return { ok: true, data: port.localContext({
          agentId: String(params.agent_id || ''),
          businessSessionId: String(params.business_session_id || ''),
        }) }
      } catch (err) {
        return refusal('device_error', String((err as Error)?.message || err))
      }
    }
    case 'bindContext': {
      if (!isRemoteLocalFilesEnabled()) {
        return refusal('feature_unavailable', 'desktop local files are not available')
      }
      const scope = (params.scope || {}) as GrantScope
      const grant = remoteGrantRegistry.get(scope)
      // The installation id is the main process's, not the page's (task 2.2):
      // a legacy page value is adopted only on the very first run so the
      // device already registered from it keeps working, and a page can never
      // swap the identity on a later confirmation.
      const identity = installationIdentityFor(
        app.getPath('userData'),
        typeof params.installationId === 'string' ? params.installationId : '',
      )
      const result = await confirmLocalContext(
        {
          scope,
          installationId: identity.id,
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
        // A server-confirmed loss of the session is the re-login entry; every
        // other refusal (a tool permission, a tenant eligibility, a network or
        // device problem) keeps its own code and is not misreported as a
        // missing login (task 2.4).
        if (isSessionInvalidCode(result.code)) return signInRequired()
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
        // Re-read both after the commit: the picker may have activated a *newer*
        // grant, and a frame issued against the older version would be a miss.
        const live = remoteGrantRegistry.get(scope)
        const committed = directorySelection.state(scope)
        if (live) {
          try {
            registration.localFiles.bound({
              origin: context.origin,
              deviceId: result.deviceId,
              workspaceId: result.workspaceId,
              grantId: live.id,
              bindingId: result.bindingId || '',
              selectionGeneration: committed.generation,
              grantVersion: live.grantVersion,
              label: live.label,
              executable: live.purpose === 'project-execution',
              // The chat this confirmation is for, so a replacement document can
              // ask for *its* project rather than the last one this container
              // happened to hold (task 9.3).
              agentId: String(params.agentId || ''),
              businessSessionId: String(params.businessSessionId || ''),
            })
          } catch (err) {
            return refusal('feature_unavailable',
              `the local directory was registered but this machine cannot serve it: ${String(err)}`)
          }
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
    case 'projectSource':
    case 'projectTree':
    case 'projectSearch':
    case 'projectResolve':
    case 'projectRead':
    case 'projectWrite': {
      if (!isRemoteLocalFilesEnabled()) {
        return refusal('feature_unavailable', 'desktop local files are not available')
      }
      const port = current.projects
      if (!port) {
        // A host without the port must *say* so. Falling through to the
        // backend's `/api/workspace/*` would answer about the server's own
        // working directory -- a different file that happens to share a name.
        return refusal('feature_unavailable', 'this host cannot read a local project')
      }
      const handlers: Record<string, (p: Record<string, unknown>) => Promise<unknown> | unknown> = {
        projectSource: () => port.describe(String(params.workspace_id || '')),
        projectTree: (p) => port.tree(p),
        projectSearch: (p) => port.search(p),
        projectResolve: (p) => port.resolve(p),
        projectRead: (p) => port.read(p),
        projectWrite: (p) => port.write(p),
      }
      try {
        return projectReply(await handlers[String(call?.method)](params))
      } catch (err) {
        return refusal('device_error', String((err as Error)?.message || err))
      }
    }
    case 'projectOpenFile':
    case 'projectRevealFile':
    case 'projectCopyPath':
    case 'projectSaveFileAs': {
      if (!isRemoteLocalFilesEnabled()) {
        return refusal('feature_unavailable', 'desktop local files are not available')
      }
      const port = current.projects
      if (!port || !port.nativeAction) {
        return refusal('feature_unavailable', 'this host cannot open a local project file')
      }
      const action = NATIVE_ACTION_BY_METHOD[String(call?.method)]
      try {
        return projectReply(await port.nativeAction(action, params))
      } catch (err) {
        return refusal('device_error', String((err as Error)?.message || err))
      }
    }
    case 'projectPreviewFile': {
      // Preview rides the same gate as the read path it is made of: with local
      // files closed there is no project to preview, and a host that cannot
      // serve one says so rather than returning a URL to nothing.
      if (!isRemoteLocalFilesEnabled()) {
        return refusal('feature_unavailable', 'desktop local files are not available')
      }
      const port = current.projects
      if (!port || !port.previewFile) {
        return refusal('feature_unavailable', 'this host cannot preview a local project file')
      }
      try {
        return projectReply(await port.previewFile(params))
      } catch (err) {
        return refusal('device_error', String((err as Error)?.message || err))
      }
    }
    case 'writeBack':
    case 'runLocalScript':
      return refuseAutomaticWriteBack()
    default:
      return refusal('feature_unavailable', 'the bridge method is not part of this bridge version')
  }
}

/**
 * Translate one browser reply into the bridge's envelope.
 *
 * The browser already speaks the bridge's vocabulary -- ``{ok:false, code,
 * message}`` for a refusal -- so this only moves the data across, keeping
 * "answered" and "refused by name" distinguishable at the page.
 */
function projectReply(reply: unknown): { ok: boolean; code?: string; message?: string; data?: unknown } {
  const value = (reply || {}) as Record<string, unknown>
  if (value.ok === false) {
    return {
      ok: false,
      code: String(value.code || 'device_error'),
      message: String(value.message || 'the local project call was refused'),
    }
  }
  return { ok: true, data: value }
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
