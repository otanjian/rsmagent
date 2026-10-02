/**
 * In-memory directory grants for the desktop local-files surface.
 *
 * Change ``add-desktop-remote-web-workbench`` (task 8.5). The absolute root the
 * user picked never leaves this process as an "active" authorization except
 * through this registry: candidates on disk are *candidates only* (task 8.5 /
 * D6), and a restart starts with an empty grant table so nothing auto-connects.
 *
 * Pure Node on purpose: the grant math (versioning, revoke, scope keys) has no
 * Electron dependency and is exercised by ``tests/test_desktop_local_files.cjs``.
 */

export interface GrantScope {
  /** Opaque server id / profile id the grant is scoped to. */
  serverId: string
  userId: string
  tenantId: string
  deviceId: string
}

/**
 * What the picked directory may be used for.
 *
 * ``readonly-input`` is the phase-2 local file reference: the device serves
 * reads for the session, nothing else. ``project-execution`` is the explicit
 * "open my project here" authorization (change
 * ``align-desktop-project-execution-with-master``): project file tools and
 * authorized Skill scripts may run in the directory.
 *
 * The two are **not** interchangeable: a read-only grant must never be widened
 * into execution by an upgrade, a refresh or a re-login, and a project-execution
 * grant does not make every local directory executable.
 */
export type GrantPurpose = 'readonly-input' | 'project-execution'

export interface ActiveGrant extends GrantScope {
  /** Local opaque id; never a path. */
  id: string
  /** Display label only — the absolute path stays private on the grant. */
  label: string
  /** Absolute root path; never serialised off-box, never sent to the server. */
  absolutePath: string
  /** Monotonic per-root version; bumped on every fresh authorization. */
  grantVersion: number
  /** Wall-clock when the grant became active (ms). */
  activatedAt: number
  /** What the user authorized the directory for. */
  purpose: GrantPurpose
}

export interface GrantPublic {
  id: string
  label: string
  grantVersion: number
  serverId: string
  userId: string
  tenantId: string
  deviceId: string
  activatedAt: number
  purpose: GrantPurpose
}

function scopeKey(scope: GrantScope): string {
  return [scope.serverId, scope.userId, scope.tenantId, scope.deviceId].join('\u0001')
}

function newId(): string {
  // 128 bits of entropy, url-safe. Matches the server-side id floor.
  const bytes = require('crypto').randomBytes(16) as Buffer
  return bytes.toString('base64url')
}

function publicOf(grant: ActiveGrant): GrantPublic {
  return {
    id: grant.id,
    label: grant.label,
    grantVersion: grant.grantVersion,
    serverId: grant.serverId,
    userId: grant.userId,
    tenantId: grant.tenantId,
    deviceId: grant.deviceId,
    activatedAt: grant.activatedAt,
    purpose: grant.purpose,
  }
}

/** Whether a grant may run project tools and scripts, not just reads. */
export function allowsProjectExecution(grant: { purpose?: GrantPurpose } | null): boolean {
  return !!grant && grant.purpose === 'project-execution'
}

/**
 * The live grant table. One active grant per scope. Clearing the table is the
 * logout / identity-switch path (task 8.7): candidates remain, activity does not.
 */
export class GrantRegistry {
  private grants = new Map<string, ActiveGrant>()
  private nextVersion = 1

  /** Activate a freshly picked root. Replaces any prior grant in the same scope. */
  activate(
    scope: GrantScope,
    absolutePath: string,
    label: string,
    purpose: GrantPurpose = 'readonly-input',
  ): GrantPublic {
    if (purpose !== 'readonly-input' && purpose !== 'project-execution') {
      throw new Error('invalid grant purpose')
    }
    if (!absolutePath || absolutePath.includes('\0')) {
      throw new Error('invalid absolute path')
    }
    // A relative path is never a picker result; refuse rather than "fix".
    if (!absolutePath.startsWith('/') && !/^[A-Za-z]:[\\/]/.test(absolutePath)) {
      throw new Error('absolute path required')
    }
    const trimmedLabel = (label || '').trim()
    if (!trimmedLabel || trimmedLabel.length > 128) {
      throw new Error('invalid label')
    }
    const key = scopeKey(scope)
    const grant: ActiveGrant = {
      ...scope,
      id: newId(),
      label: trimmedLabel,
      absolutePath,
      grantVersion: this.nextVersion++,
      activatedAt: Date.now(),
      purpose,
    }
    this.grants.set(key, grant)
    return publicOf(grant)
  }

  /** The live grant for a scope, or null. Never returns the absolute path. */
  get(scope: GrantScope): GrantPublic | null {
    const grant = this.grants.get(scopeKey(scope))
    return grant ? publicOf(grant) : null
  }

  /** The absolute path for a live grant id, or null. Main-process only. */
  absolutePathFor(grantId: string): string | null {
    for (const grant of this.grants.values()) {
      if (grant.id === grantId) return grant.absolutePath
    }
    return null
  }

  /** Drop one grant. Idempotent. */
  revoke(grantId: string): boolean {
    for (const [key, grant] of this.grants) {
      if (grant.id === grantId) {
        this.grants.delete(key)
        return true
      }
    }
    return false
  }

  /** Drop every grant in a scope (tenant/account/server change). */
  revokeScope(scope: Partial<GrantScope>): number {
    let removed = 0
    for (const [key, grant] of this.grants) {
      if (scope.serverId != null && grant.serverId !== scope.serverId) continue
      if (scope.userId != null && grant.userId !== scope.userId) continue
      if (scope.tenantId != null && grant.tenantId !== scope.tenantId) continue
      if (scope.deviceId != null && grant.deviceId !== scope.deviceId) continue
      this.grants.delete(key)
      removed += 1
    }
    return removed
  }

  /** Logout / full exit: every active grant dies. Candidates are untouched. */
  clear(): void {
    this.grants.clear()
  }

  list(): GrantPublic[] {
    return [...this.grants.values()].map(publicOf)
  }

  get size(): number {
    return this.grants.size
  }
}

/** The picker message the native dialog must show (task 8.5). */
export const PICKER_MESSAGE =
  '选择一个目录供容大AI只读访问。文件按需传输，不会在授权时上传全部内容。'

/**
 * The picker message for "open my project here" (change
 * ``align-desktop-project-execution-with-master``).
 *
 * This is a different authorization, not a widened read reference: project file
 * tools and authorized Skill scripts run in the directory. It says so plainly
 * instead of reusing the read-only wording.
 */
export const PROJECT_EXECUTION_PICKER_MESSAGE =
  '选择本机项目目录：项目文件读写与已授权技能脚本将在该目录中原地执行。'
  + '技能与记忆维护仍按原权限，不会自动上传整个目录或产出。'

/** The dialog message for a purpose. */
export function pickerMessageFor(purpose: GrantPurpose): string {
  return purpose === 'project-execution' ? PROJECT_EXECUTION_PICKER_MESSAGE : PICKER_MESSAGE
}

/**
 * Normalize a dialog result: cancel / empty → null (no grant), otherwise the
 * first selected path. The caller is responsible for never treating null as a
 * path.
 */
export function pathFromDialogResult(result: { canceled?: boolean; filePaths?: string[] } | null): string | null {
  if (!result || result.canceled) return null
  const paths = result.filePaths || []
  if (!paths.length) return null
  const chosen = paths[0]
  if (typeof chosen !== 'string' || !chosen) return null
  return chosen
}

/** Last path component, for the display label. Never the full absolute path. */
export function labelFromAbsolutePath(absolutePath: string): string {
  const trimmed = absolutePath.replace(/[\\/]+$/, '')
  const parts = trimmed.split(/[\\/]/)
  return parts[parts.length - 1] || trimmed
}
