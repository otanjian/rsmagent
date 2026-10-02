/**
 * Pure helpers for the phase-2 local-files bridge surface (task 12.1).
 *
 * No Electron import: tests drive the decision table without a dialog.
 */

import {
  GrantRegistry,
  PICKER_MESSAGE,
  pickerMessageFor,
  pathFromDialogResult,
  labelFromAbsolutePath,
} from '../local-files/grants'
import type { GrantScope, GrantPurpose, GrantPublic } from '../local-files/grants'
import { rememberCandidate } from '../local-files/candidates'
import type { CandidatesFile } from '../local-files/candidates'
import { PHASE1_METHODS, ALL_BRIDGE_METHODS } from './host-bridge'

export { PICKER_MESSAGE }

/** Process-wide grant table for the remote container (one per app lifetime). */
export const remoteGrantRegistry = new GrantRegistry()

/**
 * Whether the remote bridge may open the native directory picker.
 *
 * Set from the server's public meta (`features.local_files.available`) when the
 * container attaches. Defaults to false so a host that never probed still
 * refuses rather than offering a picker that cannot bind.
 */
let remoteLocalFilesEnabled = false

export function setRemoteLocalFilesEnabled(enabled: boolean): void {
  remoteLocalFilesEnabled = !!enabled
}

export function isRemoteLocalFilesEnabled(): boolean {
  return remoteLocalFilesEnabled
}

/** Shape of ``getCapabilities`` once local-files opens (or stays closed). */
export function bridgeCapabilitiesPayload(opts: {
  bridge: string
  generation: number
  saveAsApproval: unknown
}): {
  bridge: string
  methods: string[]
  allMethods: string[]
  generation: number
  localFiles: boolean
  saveAsApproval: unknown
} {
  const localFiles = remoteLocalFilesEnabled
  return {
    bridge: opts.bridge,
    // Phase 3 (the project *source* the panel reads and edits) needs a bound
    // local directory to be useful at all, so it opens with local-files rather
    // than with a switch of its own. Whether a given project may be *edited* is
    // decided per call from the grant's purpose -- a read-only grant is not
    // widened by the surface being present.
    methods: localFiles
      ? [...ALL_BRIDGE_METHODS]
      : [...PHASE1_METHODS],
    allMethods: [...ALL_BRIDGE_METHODS],
    generation: opts.generation,
    localFiles,
    saveAsApproval: opts.saveAsApproval,
  }
}

export interface ChooseWorkspaceParams {
  scope: GrantScope
  /** Dialog result injected by the dispatcher (or a test). */
  dialogResult: { canceled?: boolean; filePaths?: string[] } | null
  /** Mutable candidates file the dispatcher loaded from disk. */
  candidates: CandidatesFile
  /**
   * What the user is opening the directory for. Defaults to the phase-2
   * read-only reference so an older caller cannot silently gain execution.
   */
  purpose?: GrantPurpose
}

/** Normalize a page-supplied purpose; anything unknown is the read-only default. */
export function normalizeGrantPurpose(value: unknown): GrantPurpose {
  return value === 'project-execution' ? 'project-execution' : 'readonly-input'
}

/** The dialog message for a purpose (dispatcher picks before showing it). */
export function chooseWorkspaceMessage(purpose: GrantPurpose): string {
  return pickerMessageFor(purpose)
}

export type ChooseWorkspaceResult =
  | { ok: true; grant: GrantPublic; activated: true }
  | { ok: true; activated: false; reason: 'cancelled' }
  | { ok: false; code: string; message: string }

/**
 * Activate a picked root, and remember it as a candidate.
 *
 * Split out from :func:`applyChooseWorkspace` because the *selection service*
 * (task 2.3/2.4) needs to commit a root it obtained itself -- through the same
 * generation, cancel and ready-state bookkeeping -- without pretending it has a
 * dialog result to re-parse. Both entry points therefore commit through one
 * function, so a purpose cannot be widened in one of them and not the other.
 */
export function activateGrant(params: {
  scope: GrantScope
  absolutePath: string
  purpose?: GrantPurpose
  candidates: CandidatesFile
}): { ok: true; grant: GrantPublic } | { ok: false; code: string; message: string } {
  const scope = params.scope
  if (!scope || !scope.serverId || !scope.userId || !scope.tenantId || !scope.deviceId) {
    return { ok: false, code: 'invalid_request', message: 'chooseWorkspace requires a full scope' }
  }
  const absolute = params.absolutePath
  if (!absolute) {
    return { ok: false, code: 'invalid_request', message: 'a selection needs an absolute path' }
  }
  const label = labelFromAbsolutePath(absolute)
  let grant: GrantPublic
  try {
    grant = remoteGrantRegistry.activate(scope, absolute, label, params.purpose || 'readonly-input')
  } catch (err: any) {
    return { ok: false, code: 'invalid_request', message: String((err && err.message) || err) }
  }
  const next = rememberCandidate(params.candidates, {
    absolutePath: absolute,
    label,
    serverId: scope.serverId,
    userId: scope.userId,
    tenantId: scope.tenantId,
    deviceId: scope.deviceId,
  })
  // Mutate the caller's file in place so the dispatcher can persist it.
  params.candidates.version = next.version
  params.candidates.candidates = next.candidates
  return { ok: true, grant }
}

export function applyChooseWorkspace(params: ChooseWorkspaceParams): ChooseWorkspaceResult {
  const scope = params.scope
  if (!scope || !scope.serverId || !scope.userId || !scope.tenantId || !scope.deviceId) {
    return { ok: false, code: 'invalid_request', message: 'chooseWorkspace requires a full scope' }
  }
  const absolute = pathFromDialogResult(params.dialogResult)
  if (!absolute) {
    return { ok: true, activated: false, reason: 'cancelled' }
  }
  const activated = activateGrant({
    scope,
    absolutePath: absolute,
    purpose: params.purpose,
    candidates: params.candidates,
  })
  if (!activated.ok) return activated
  return { ok: true, grant: activated.grant, activated: true }
}

export function applyDisconnectWorkspace(scope?: Partial<GrantScope>): {
  ok: true
  revoked: number
} {
  if (!scope || (!scope.serverId && !scope.userId && !scope.tenantId && !scope.deviceId)) {
    const n = remoteGrantRegistry.size
    remoteGrantRegistry.clear()
    return { ok: true, revoked: n }
  }
  return { ok: true, revoked: remoteGrantRegistry.revokeScope(scope) }
}

/**
 * Action-approval applicability for ordinary user save-as (task 12.3 / F17).
 *
 * Save-as is a user-initiated download of the caller's own artifact. It is
 * **not** an enterprise high-risk action: the record is ``not_applicable`` so
 * auditors see why no approval ticket was opened. Automatic write-back and
 * local scripts remain refused elsewhere.
 */
export function saveAsApprovalApplicability(): {
  action: string
  applicable: false
  reason: string
} {
  return {
    action: 'desktop.save_as',
    applicable: false,
    reason: 'user-initiated save of own artifact; not an enterprise high-risk write-back',
  }
}

/** Automatic write-back / local script requests are flatly refused. */
export function refuseAutomaticWriteBack(): {
  ok: false
  code: 'permission_denied'
  message: string
} {
  return {
    ok: false,
    code: 'permission_denied',
    message: 'automatic local write-back and local scripts are not allowed',
  }
}
