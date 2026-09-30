/**
 * Desktop lifecycle decisions for phase-3 A (tasks 13.1–13.4).
 *
 * Pure Node: close-to-tray preference, wake revalidation policy, and
 * notification_ref click identity checks — driven by
 * ``tests/test_desktop_lifecycle.cjs`` without Electron.
 */

export type CloseBehavior = 'tray' | 'quit'

export interface LifecyclePreferences {
  /** Default ``tray`` (驻留). User must opt into ``quit`` on window close. */
  closeBehavior: CloseBehavior
  /** Autostart is always opt-in; this flag only mirrors the OS login-item. */
  launchAtLogin: boolean
}

export const DEFAULT_LIFECYCLE_PREFERENCES: LifecyclePreferences = {
  closeBehavior: 'tray',
  launchAtLogin: false,
}

export function shouldCloseToTray(
  prefs: LifecyclePreferences,
  opts: { isQuitting: boolean },
): boolean {
  if (opts.isQuitting) return false
  return prefs.closeBehavior === 'tray'
}

export type WakeAction =
  | { action: 'revalidate'; reason: string }
  | { action: 'noop' }

/**
 * After system resume, always revalidate session / lease / source version
 * before continuing remote file work. Unknown in-flight business writes are
 * never auto-resent from this layer (task 13.2).
 */
export function wakePolicy(event: 'resume' | 'unlock-screen' | 'suspend'): WakeAction {
  if (event === 'suspend') return { action: 'noop' }
  return { action: 'revalidate', reason: event }
}

export interface NotificationRef {
  /** Opaque server-issued id; never a path or token. */
  notification_ref: string
  serverId: string
  userId: string
  tenantId?: string
  resourceKind: 'scheduler_run' | 'agent_run'
  resourceId: string
  /** Summary only — no file body / absolute path. */
  summary: string
}

export interface NotificationClickContext {
  activeServerId: string | null
  activeUserId: string | null
  activeTenantId: string | null
}

export type NotificationClickVerdict =
  | { ok: true; resourceKind: string; resourceId: string }
  | { ok: false; code: string; message: string }

/**
 * Clicking a notification must not open a session for a different
 * server/account/tenant (task 13.4).
 */
export function verdictForNotificationClick(
  ref: NotificationRef,
  ctx: NotificationClickContext,
): NotificationClickVerdict {
  if (!ref || !ref.notification_ref || !ref.resourceId) {
    return { ok: false, code: 'invalid_request', message: 'notification_ref is incomplete' }
  }
  if (!ctx.activeServerId || ctx.activeServerId !== ref.serverId) {
    return { ok: false, code: 'stale_context', message: 'notification belongs to another server' }
  }
  if (!ctx.activeUserId || ctx.activeUserId !== ref.userId) {
    return { ok: false, code: 'stale_context', message: 'notification belongs to another account' }
  }
  if (ref.tenantId && ctx.activeTenantId && ref.tenantId !== ctx.activeTenantId) {
    return { ok: false, code: 'stale_context', message: 'notification belongs to another tenant' }
  }
  // Summaries must stay free of absolute paths / secrets.
  const summary = String(ref.summary || '')
  if (
    /^[A-Za-z]:[\\/]/.test(summary) ||
    summary.startsWith('/Users/') ||
    summary.startsWith('/home/') ||
    summary.includes('\\Users\\')
  ) {
    return { ok: false, code: 'unsafe_path', message: 'notification summary must not carry paths' }
  }
  return { ok: true, resourceKind: ref.resourceKind, resourceId: ref.resourceId }
}

/** Dedupe key: server + user + resource event (task 13.3). */
export function notificationDedupeKey(ref: NotificationRef): string {
  return [ref.serverId, ref.userId, ref.resourceKind, ref.resourceId].join('\u0001')
}
