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

import { BrowserWindow, app, clipboard, dialog, ipcMain, protocol, session as electronSession, shell } from 'electron'
import { randomBytes } from 'crypto'
import * as fs from 'fs'
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
import { remoteGrantRegistry, saveAsApprovalApplicability } from './local-files-bridge'
import { LocalReadAssembly } from './local-read-assembly'
import { localGatewayPort } from './device-connection'
import { ProjectBrowser, type ProjectBinding } from '../project-browser/browser'
import type { LocalProjectRecord } from '../project-browser/restore'
import { restoreLocalContext } from '../project-browser/restore'
import { ProjectWatcher } from '../project-browser/watch'
import {
  performNativeAction, planNativeAction,
  type NativeAction, type NativeActionEffects,
} from '../project-browser/native-actions'
import { resolveGuardBinary, spawnGuardProcess } from '../local-files/fs-guard-binary'
import { materializeLocalFile, type MaterializeTransport } from '../local-files/materialize'
import { createMaterializeTransport } from './materialize-transport'
import {
  PREVIEW_SCHEME, PREVIEW_SCHEME_PRIVILEGES, answerPreviewRequest, createPreviewTickets,
  planLocalPreview, readLocalPreviewContent, type PreviewTicketStore,
} from '../project-browser/preview'
import { resolveBackendPath } from '../backend-path'
import { InterpreterRegistry, findWorkerRuntime } from '../local-execution/interpreter'
import { registerRemoteHost, setupRemoteHostIPC, teardownRemoteHostIPC } from './remote-host-ipc'
import { remoteCoveringScript } from './shell-covering'
import { partitionName, isValidInstanceId } from './web-session'
import { SignOutCoordinator, type SignOutOutcome } from './session-signout'

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

/**
 * The local shell's sign-out feed (change ``fix-desktop-relogin-session-sync``,
 * task 2.2).
 *
 * Sent to the *local* renderer -- never to the container -- so the shell learns
 * that a sign-out started and how it ended even though the document that asked
 * for it is destroyed by the host before the reply settles. The shell reacts by
 * refreshing its own login/connection entry; the payload carries the phase and
 * the outcome only, never an account, a token or a session snapshot.
 */
export const CHANNEL_LOGOUT_STATE = 'desktop:logout-state'

export interface LogoutStateNotice {
  phase: 'started' | 'finished'
  ok: boolean
  revoked: boolean
  code: string
  message: string
}

/** Tell the local shell about a sign-out phase. Best-effort: it may be gone. */
function emitLogoutState(window: BrowserWindow | null, notice: LogoutStateNotice): void {
  if (!window || window.isDestroyed()) return
  try {
    window.webContents.send(CHANNEL_LOGOUT_STATE, notice)
  } catch {
    /* the shell is already gone */
  }
}

/**
 * Make the preview scheme a real, trustworthy scheme (task 9.5).
 *
 * Three privileges, each doing one job, and none of them a relaxation of the
 * preview's own policy:
 *
 *   * `standard` -- the URL parses like an ordinary one, so the *frame's* origin
 *     is the scheme and host the handler answers for, not `null`;
 *   * `secure` -- the console page is a secure context, and a scheme Chromium
 *     does not consider trustworthy would be refused there as mixed content;
 *   * `stream` -- a preview may be answered with a streamed body.
 *
 * The list itself is `PREVIEW_SCHEME_PRIVILEGES`, shared with the isolation probe
 * so what is measured is what is shipped.
 *
 * Must run before `app.ready`: after that Electron has already built its scheme
 * registry and this call is a no-op with a warning.
 */
protocol.registerSchemesAsPrivileged([
  { scheme: PREVIEW_SCHEME, privileges: PREVIEW_SCHEME_PRIVILEGES },
])

let container: RemoteContainer | null = null
let attachedPartition = ''
/** Window whose shell is covered by the active container (for drag passthrough). */
let attachedWindow: BrowserWindow | null = null
/** Disposers for what was installed on the current partition's session. */
let partitionDisposers: (() => void)[] = []
/** Content windows opened by this container; closed when it detaches. */
let contentWindows: BrowserWindow[] = []

/**
 * The live preview tickets for this attach (task 9.5).
 *
 * Created per attach and emptied on detach, so a URL that survived a mode switch
 * cannot be spent on the next one. What authorizes a read is the ticket and
 * nothing else -- no cookie, no path, no session -- which is why dropping the
 * table is the same thing as revoking every preview at once.
 */
let previewTickets: PreviewTicketStore = createPreviewTickets()

/**
 * Install the preview scheme on the container's session.
 *
 * The handler is per session, and this is the session the *page* runs in: a
 * preview URL is embedded by the container document, so the scheme has to answer
 * there. Registration is not free of consequences -- a scheme that is handled but
 * not privileged is a scheme Chromium refuses to load from a secure page -- which
 * is why the privileges are declared once, at import, next to this.
 */
function installPreviewProtocol(partition: string): void {
  const target = electronSession.fromPartition(partition)
  void target.protocol.handle(PREVIEW_SCHEME, (request) => {
    const answer = answerPreviewRequest(request.url, previewTickets)
    return new Response(answer.body, { status: answer.status, headers: answer.headers })
  })
  partitionDisposers.push(() => {
    void target.protocol.unhandle(PREVIEW_SCHEME)
  })
}

/**
 * The local read path for the live container (change
 * ``fix-desktop-local-context-and-tool-calls``, task 2.4): the helper process,
 * the workspace map, and the device connection.
 *
 * Created per attach and disposed on detach, so the device is reachable exactly
 * while a container is bound to a directory and nothing survives a mode switch.
 */
let localRead: LocalReadAssembly | null = null

/**
 * The local project **source** behind the file panel (task 9.2).
 *
 * Built per attach beside {@link localRead}, sharing its device connection: the
 * panel reads through the same `list`/`stat`/`search`/`read_text` handler the v1
 * channel uses and edits through the same v2 execution channel the model's own
 * `write` call uses. It is deliberately *not* a second filesystem stack -- the
 * panel and the model see one project, with one set of clamps and one journal.
 */
let projectBrowser: ProjectBrowser | null = null

/**
 * What this device knows about each bound workspace, as of its confirmation.
 *
 * Written once when `bindContext` commits (that is the only moment the server
 * binding id, the selection generation and the grant version are all in hand)
 * and read on every panel call, so a re-pick or a revoke is refused by the next
 * call rather than served from a stale snapshot. The live half of a binding --
 * grant version, connection epoch, connectedness -- is re-read on every call and
 * never stored here.
 */
type RecordedProjectBinding = Pick<ProjectBinding,
  'deviceId' | 'selectionGeneration' | 'grantVersion' | 'label' | 'executable'>
  & LocalProjectRecord

const projectBindings = new Map<string, RecordedProjectBinding>()

/**
 * The panel's bounded watcher over the bound project (task 9.3).
 *
 * Started when a project is confirmed and stopped the moment its authorization
 * goes away, so a revoked or re-picked directory leaves no scan running and no
 * remembered listing behind.
 */
let projectWatcher: ProjectWatcher | null = null

/**
 * The live binding for a workspace, or ``null``.
 *
 * One function for both callers -- the browser that serves the panel and the
 * watcher that polls it -- so "is this project still authorized" has exactly one
 * answer in this process. The recorded half is written once at confirmation; the
 * live half (grant version, connection epoch, connectedness) is re-read here on
 * every call, never trusted from the snapshot: a grant revoked between two calls
 * must stop being readable *now*.
 */
function liveProjectBinding(workspaceId: string): ProjectBinding | null {
  const recorded = projectBindings.get(workspaceId)
  if (!recorded) return null
  const live = remoteGrantRegistry.list().find((grant) => grant.id === recorded.grantId)
  if (!live) {
    // A revoked grant is gone from the registry; keeping its record would only
    // be a stale label and a path-less root nothing can serve.
    projectBindings.delete(workspaceId)
    return null
  }
  const connection = localRead?.connectionSnapshot() ?? { connected: false, epoch: '' }
  // The chat the confirmation belonged to stays here: it answers a question
  // about the *page*, and a binding that carried it into every panel call would
  // be publishing a session id to code that has no use for one.
  const { agentId: _agentId, businessSessionId: _businessSessionId, ...binding } = recorded
  return {
    ...binding,
    grantVersion: live.grantVersion,
    label: live.label,
    executable: live.purpose === 'project-execution',
    connectionEpoch: connection.epoch,
    connected: connection.connected,
  }
}

/**
 * The live local project for one chat, or why there is none (task 9.3).
 *
 * This is what a *replacement document* asks after a reload: the page's own
 * variables are gone, but the confirmation is not, and re-opening the project
 * must not mean re-picking the folder. The decision itself is pure (``restore``)
 * so its two properties -- derived from the live registry, and only ever about
 * this chat -- can be tested without Electron; what stays here is the record
 * table it reads and the watch it starts.
 */
function liveLocalContext(request: { agentId: string; businessSessionId: string }): unknown {
  const answer = restoreLocalContext(
    request,
    [...projectBindings.values()],
    (workspaceId) => liveProjectBinding(workspaceId),
  )
  // A restored project is watched like a freshly opened one: the watch may have
  // been stopped while no document was showing the project, and the document
  // that just came back has to be told about changes.
  if (answer.state === 'live') projectWatcher?.start(answer.binding.workspace_id)
  return answer
}

/**
 * Run one panel call against this attach's browser, or refuse.
 *
 * A call can arrive with no browser -- the page has a live document between
 * attach and detach but the project was never opened. Answering those with a
 * thrown error would surface as `device_error`, which reads like a broken file
 * rather than "no project is open here"; the browser's own vocabulary is used
 * instead so the panel can say which one it is.
 */
function withBrowser(call: (browser: ProjectBrowser) => unknown): unknown {
  const browser = projectBrowser
  if (!browser) {
    return {
      ok: false,
      code: 'stale_context',
      message: 'no local project is open in this session',
    }
  }
  return call(browser)
}

/**
 * The native effects these actions are carried out with (task 9.4).
 *
 * Electron's own calls, in one object: in the container the actions *are* the
 * system, so there is nothing to abstract over -- what the tests need to drive
 * is the decision, which is ``planNativeAction``'s, not this.
 */
const nativeEffects: NativeActionEffects = {
  openPath: (absolutePath) => shell.openPath(absolutePath),
  revealPath: (absolutePath) => {
    shell.showItemInFolder(absolutePath)
  },
  // On *this* machine's clipboard, so the path is never sent to the page.
  copyPath: (absolutePath) => clipboard.writeText(absolutePath),
  chooseDestination: async (suggestedName) => {
    const choice = await dialog.showSaveDialog({
      title: suggestedName,
      defaultPath: suggestedName,
    })
    return choice.canceled || !choice.filePath ? null : choice.filePath
  },
  copyFile: async (source, destination) => {
    await fs.promises.copyFile(source, destination)
  },
}

/**
 * Open / reveal / copy-path / save-as for one file of a bound project (9.4).
 *
 * Two verifications happen on *every* call and neither is cached:
 *
 *   * ``browser.resolve`` asks the guard helper about this exact path, which is
 *     the same authorization the panel read the file with -- a revoked grant or
 *     a deleted file refuses here, with the helper's own code;
 *   * ``planNativeAction`` stats and resolves the path in this process, because
 *     the OS is about to follow it and the helper's answer describes a *read*
 *     through its own root, not what ``openPath`` will touch.
 *
 * The absolute path is used, not returned: the reply carries a file name.
 */
function projectNativeAction(action: NativeAction, params: Record<string, unknown>): unknown {
  return withBrowser(async (browser) => {
    const workspaceId = String(params.workspace_id || '')
    const entry = await browser.resolve({ workspace_id: workspaceId, path: params.path })
    const recorded = projectBindings.get(workspaceId)
    const plan = planNativeAction({
      kind: action,
      path: params.path,
      // The live grant's directory, read from the registry *now*: the recorded
      // binding keeps the grant id, and a grant that was replaced since carries
      // a different id and answers ``null`` here.
      root: recorded ? remoteGrantRegistry.absolutePathFor(recorded.grantId) : null,
      entry: (entry || {}) as Record<string, unknown>,
    })
    if (!plan.ok) return plan
    const result = await performNativeAction(plan, nativeEffects, {
      expectedMtime: typeof params.expected_mtime === 'number' ? params.expected_mtime : undefined,
      acceptCurrent: params.accept_current === true,
    })
    if (result.ok && result.kind === 'saveAs') {
      // Applicability is recorded exactly as ``saveArtifact`` records it: a
      // project save is not a lower tier of action, and an auditor asking "why
      // was no approval ticket opened" gets the same answer for both.
      return { ...result, approval: saveAsApprovalApplicability() }
    }
    return result
  })
}

/**
 * The isolated preview of one file of a bound project (task 9.5).
 *
 * The page names a file. This answers with a refusal, or with a URL it may
 * *embed* while the ticket lives, and nothing else:
 *
 *   * `browser.resolve` and `planLocalPreview` re-verify on every call, exactly
 *     as the system actions do -- a revoked grant, a link, a deleted file or a
 *     file that is no longer the kind it was answered by name;
 *   * the bytes are read here, bounded, and parked in a ticket rather than sent
 *     to the page: a preview is a URL that expires, not a copy of the file;
 *   * the reply carries the file's *name*, its kind, and when the URL stops
 *     working. There is no field a directory could hide in.
 */
function projectPreviewFile(params: Record<string, unknown>): unknown {
  return withBrowser(async (browser) => {
    const workspaceId = String(params.workspace_id || '')
    const entry = await browser.resolve({ workspace_id: workspaceId, path: params.path })
    const recorded = projectBindings.get(workspaceId)
    const plan = planLocalPreview({
      path: params.path,
      // The live grant's directory, read now: the recorded binding keeps the
      // grant id, and a grant that was replaced carries a different id and
      // answers ``null`` here.
      root: recorded ? remoteGrantRegistry.absolutePathFor(recorded.grantId) : null,
      entry: (entry || {}) as Record<string, unknown>,
    })
    if (!plan.ok) return plan
    const read = await readLocalPreviewContent({ absolutePath: plan.absolutePath })
    if (read.ok !== true) return read
    const ticket = previewTickets.issue({
      content: read.content,
      name: plan.name,
      kind: plan.kind,
    })
    return {
      ok: true,
      name: ticket.name,
      kind: ticket.kind,
      size: ticket.size,
      // Short-lived, single-file, and inert for script: see ``preview.ts``.
      url: ticket.url,
      expires_at: ticket.expiresAt,
    }
  })
}

/** A diagnostics-only report of the device connection state. */
function noteDeviceState(state: string, detail?: string): void {
  if (state === 'ready') console.log('[desktop] device connection ready')
  else if (state === 'reconnecting') console.warn(`[desktop] device connection reconnecting: ${detail || ''}`)
  else if (state === 'stopped') console.log('[desktop] device connection stopped')
}

/**
 * An unsettled run, surfaced where an operator can see it (task 7.3).
 *
 * Identifiers only: the workspace is the server's id and the directory is never
 * printed, because this line can end up in a log that leaves the machine. What
 * it says is the one thing that matters -- a command began, no completion was
 * recorded, and it is not retried.
 */
function noteExecutionReconcile(entry: {
  journal_id: string
  command_id: string
  run_id: string
  tool: string
  workspace_id: string
}, reason: string): void {
  console.warn(
    `[desktop] execution needs reconciliation: journal=${entry.journal_id} `
    + `command=${entry.command_id} run=${entry.run_id} tool=${entry.tool} `
    + `workspace=${entry.workspace_id} -- ${reason}`,
  )
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
 * The one sign-out the container may start, serialised.
 *
 * ``detachRemoteContainer`` is the whole operation (block, then revoke), so the
 * coordinator only adds what was missing: merging a repeated call and refusing
 * a reconnect while one is unresolved or unconfirmed (task 2.1). It also emits
 * the local shell's start/end notices, because the container document that
 * called this is destroyed by the detach itself.
 */
const signOutCoordinator = new SignOutCoordinator(performContainerSignOut)

async function performContainerSignOut(): Promise<SignOutOutcome> {
  // Capture the shell before the detach clears it: the "finished" notice has to
  // reach a window that is not the one being torn down.
  const shell = attachedWindow
  emitLogoutState(shell, { phase: 'started', ok: false, revoked: false, code: '', message: '' })
  const result = await detachRemoteContainer()
  emitLogoutState(shell, {
    phase: 'finished',
    ok: result.ok,
    revoked: result.revoked,
    code: result.ok ? '' : 'logout_incomplete',
    message: result.message,
  })
  return result
}

/**
 * Why a new container must not be attached right now, or ``''``.
 *
 * Two sources: the coordinator (a sign-out in flight or unconfirmed) and the
 * broker's own blocked reason (the same ``logout_incomplete`` when the server
 * never confirmed). Either way the workbench must not re-authorize past a
 * session that was asked to end and was not confirmed to have ended.
 */
function attachRefusal(): string {
  const coordinated = signOutCoordinator.attachRefusal()
  if (coordinated) return coordinated
  return status().blockedReason
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
  // A sign-out that is running (or that the server never confirmed) blocks a
  // reconnect: re-authorizing past it would let a session the user asked to end
  // keep working (task 2.1).
  const refused = attachRefusal()
  if (refused) {
    return {
      ok: false,
      code: refused,
      message: refused === 'logout_in_progress'
        ? 'a sign-out is still in progress'
        : 'the previous sign-out was not confirmed; retry it before reconnecting',
    }
  }
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
  // The preview scheme lives on this attach's session and its tickets live in
  // this process; both are created here so neither can outlive the container.
  previewTickets = createPreviewTickets()
  installPreviewProtocol(child.partition)
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
  // The v2 execution channel (group 7). The interpreter is resolved once here,
  // and probed lazily by the registry: a build without an interpreter still
  // offers the pipeline and refuses each frame with a real reason, rather than
  // pretending the capability is absent.
  const backendPath = resolveBackendPath({
    dev: !app.isPackaged,
    resourcesPath: process.resourcesPath,
    moduleDir: __dirname,
  })
  const interpreters = new InterpreterRegistry()
  const workerRuntime = findWorkerRuntime(backendPath)
  // One transport for this container's lifetime, so the origin is validated
  // once against the configured value rather than per upload.
  const uploadTransport = createMaterializeTransport({
    origin: options.origin,
    // Read per call: a reconnect or a re-login must not reuse a dead bearer.
    token: async () => (status().session ? nativeBearer() : null),
    tenantId: () => status().session?.tenantId || null,
    fetch,
    allowHttpOrigin: getLocalBackendOrigin() || undefined,
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
    localGatewayPort: () => localGatewayPort(getLocalBackendOrigin()),
    onState: noteDeviceState,
    execution: {
      // Under the app's own user-data directory: the journal is the only record
      // that a command began here, so it cannot live somewhere a cleanup pass
      // could sweep.
      journalFile: path.join(app.getPath('userData'), 'project-execution-journal.json'),
      // Beside the journal, and for the same reason: a verified skill version
      // is the proof that this device may run a run's tools, so it lives under
      // the app's own user-data directory rather than somewhere swept.
      skillCacheRoot: path.join(app.getPath('userData'), 'skill-cache'),
      backendPath,
      // One resolver for both shapes: an interpreter in a checkout, the app's own
      // bundle binary in an installed build. A null here is answered as a real
      // refusal per frame rather than as "this build has no such capability".
      runtime: workerRuntime ?? null,
      probe: (candidate) => interpreters.get(candidate),
      platform: process.platform,
      seatbeltAvailable: fs.existsSync('/usr/bin/sandbox-exec'),
      baseEnv: process.env,
    },
    onReconcile: noteExecutionReconcile,
    // The explicit hand-over (task 9.7). Nothing else in this process uploads a
    // local file: the transport exists so a `materialize` command the server
    // queued can be honoured, and a build whose origin cannot be trusted gets a
    // refusal from `createMaterializeTransport` instead of a silent send.
    materialize: ({ commandId, workspaceId, relativePath, expectedVersion, guard, fsGrant }) =>
      materializeLocalFile({
        guard,
        fsGrant,
        relativePath,
        expectedVersion,
        commandId,
        workspaceId,
        transport: uploadTransport,
      }).then((outcome) => ({ ...outcome })),
  })
  // The panel's source of truth for a local project (task 9.2). It reads and
  // edits through the *same* assembly the device channel uses, so the panel
  // inherits its clamps, its per-project serialisation and its journal; a build
  // without project execution refuses an edit by name instead of writing around
  // the runner.
  projectBindings.clear()
  projectBrowser = new ProjectBrowser({
    commands: {
      runCommand: (command) => {
        const assembly = localRead
        if (!assembly) {
          return Promise.resolve({
            state: 'failed',
            errorCode: 'feature_unavailable',
            errorMessage: 'the local directory is not open in this session',
          })
        }
        return assembly.runCommand(command)
      },
    },
    binding: (workspaceId) => liveProjectBinding(workspaceId),
    executeTool: (frame) => {
      const assembly = localRead
      if (!assembly) {
        return Promise.resolve({
          state: 'failed',
          error_code: 'feature_unavailable',
          error_message: 'the local directory is not open in this session',
        })
      }
      return assembly.runExecution(frame)
    },
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
    // The container's own sign-out: single-flighted, and refused while one is
    // unresolved so the shell cannot reconnect past an unconfirmed end.
    signOut: () => signOutCoordinator.signOut(),
    localFiles: {
      bound: (bound) => {
        // The confirmation named a server workspace and a local grant; hand
        // both to the read path so a command for that workspace can run.
        if (!localRead) return
        if (!localRead.bindDevice(bound)) {
          throw new Error('the local directory could not be attached to the device connection')
        }
        // Record the panel's half of the same confirmation: the ids an edit
        // frame has to name, captured while they are known together.
        projectBindings.set(bound.workspaceId, {
          workspaceId: bound.workspaceId,
          grantId: bound.grantId,
          deviceId: bound.deviceId,
          bindingId: bound.bindingId,
          selectionGeneration: bound.selectionGeneration,
          grantVersion: bound.grantVersion,
          label: bound.label,
          executable: bound.executable,
          agentId: bound.agentId,
          businessSessionId: bound.businessSessionId,
        })
        // The panel's watcher follows the same confirmation: this is the only
        // moment the binding id and the selection generation are known together,
        // and a watch started before it would have no scope to verify against.
        projectWatcher?.start(bound.workspaceId)
      },
      revoked: (all) => {
        if (!localRead) return
        if (all) {
          localRead.dispose()
          projectBindings.clear()
          projectWatcher?.stop('stale_context')
          return
        }
        // A revocation may or may not have taken *this* project: the watcher
        // re-reads the live binding and stops itself if it is gone.
        projectWatcher?.verifyScope()
      },
      localContext: (request) => liveLocalContext(request),
    },
    projects: {
      // One delegate per method, so the bridge's own validation stays the only
      // place a page's parameters are trusted. A call that arrives with no
      // browser (between attach and detach) is refused by name rather than
      // thrown at: an exception would read as a broken file, not a closed one.
      describe: (workspaceId) => withBrowser((browser) => browser.describe(workspaceId)),
      tree: (params) => withBrowser((browser) => browser.tree(params)),
      search: (params) => withBrowser((browser) => browser.search(params)),
      resolve: (params) => withBrowser((browser) => browser.resolve(params)),
      read: (params) => withBrowser((browser) => browser.read(params)),
      write: (params) => withBrowser((browser) => browser.write(params)),
      nativeAction: (action, params) => projectNativeAction(action, params),
      previewFile: (params) => projectPreviewFile(params),
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
  // The panel's own watcher (task 9.3): the same reader the panel uses, the same
  // binding lookup, and the host event channel it already listens on. Created
  // with the browser and before any binding can be confirmed, so the first
  // ``bound`` has something to start.
  projectWatcher = new ProjectWatcher({
    reader: {
      tree: (params) => withBrowser((browser) => browser.tree(params)) as Promise<unknown>,
    },
    scope: (workspaceId) => {
      const binding = liveProjectBinding(workspaceId)
      if (!binding) return null
      return {
        workspaceId: binding.workspaceId,
        bindingId: binding.bindingId,
        selectionGeneration: binding.selectionGeneration,
        grantVersion: binding.grantVersion,
        connectionEpoch: binding.connectionEpoch,
      }
    },
    emit: (event) => view.webContents.send('desktop:bridge:event', event),
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
  // The panel's source goes with it. Dropped *after* the assembly is disposed
  // and before the bridge stops answering, so no call can observe a browser
  // whose device connection has already been torn down.
  projectBrowser = null
  projectBindings.clear()
  // The watch goes before the bridge stops answering: a scan that outlived its
  // authorization would keep asking the device about a directory whose binding
  // is being revoked, and the page would keep refreshing a project it no longer
  // has open.
  projectWatcher?.stop()
  projectWatcher = null
  // Every preview URL this attach handed out stops answering now: the tickets
  // are the authorization, and there is no reason for one to survive the
  // container that minted it. The protocol handler itself is removed with the
  // partition's disposers below.
  previewTickets.revokeAll()
  previewTickets = createPreviewTickets()
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
