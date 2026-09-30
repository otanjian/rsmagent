/**
 * Pure helpers for the phase-2 local-files bridge surface (task 12.1).
 *
 * No Electron import: tests drive the decision table without a dialog.
 */

import {
  GrantRegistry,
  PICKER_MESSAGE,
  pathFromDialogResult,
  labelFromAbsolutePath,
} from '../local-files/grants'
import type { GrantScope, GrantPublic } from '../local-files/grants'
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
    methods: localFiles ? [...ALL_BRIDGE_METHODS] : [...PHASE1_METHODS],
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
}

export type ChooseWorkspaceResult =
  | { ok: true; grant: GrantPublic; activated: true }
  | { ok: true; activated: false; reason: 'cancelled' }
  | { ok: false; code: string; message: string }

export function applyChooseWorkspace(params: ChooseWorkspaceParams): ChooseWorkspaceResult {
  const scope = params.scope
  if (!scope || !scope.serverId || !scope.userId || !scope.tenantId || !scope.deviceId) {
    return { ok: false, code: 'invalid_request', message: 'chooseWorkspace requires a full scope' }
  }
  const absolute = pathFromDialogResult(params.dialogResult)
  if (!absolute) {
    return { ok: true, activated: false, reason: 'cancelled' }
  }
  const label = labelFromAbsolutePath(absolute)
  const grant = remoteGrantRegistry.activate(scope, absolute, label)
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
  return { ok: true, grant, activated: true }
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
