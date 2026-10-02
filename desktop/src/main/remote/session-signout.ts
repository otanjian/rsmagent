// The one sign-out the container may start, and how it is serialised.
//
// Change ``fix-desktop-relogin-session-sync``, tasks 1.2 / 2.1. The desktop
// workbench used to sign out by reloading into the ordinary Web password form:
// the native session and its paired Web session were both revoked, but the
// page came back as a plain Web login and looked signed in while the host had
// no native session, so every directory bind answered ``not signed in``.
//
// The fix keeps the account entry on the *native* path, and this module is the
// part of it that has to be right even when it is called twice:
//
//   * repeated sign-outs merge into one detach/logout, because two competing
//     sign-outs would race the same partition and the same server session;
//   * while a sign-out is unresolved, and while one has failed, the workbench
//     must not re-authorize or re-attach -- continuing to act on a session the
//     server never revoked is exactly the failure the broker already forbids.
//
// It is deliberately pure (no Electron import) so ``tests/test_desktop_signout.cjs``
// drives the merge and the block with deferred promises instead of a running app.

export type SignOutOutcome = { ok: boolean; revoked: boolean; message: string }

/**
 * Why a new attach/authorization must be refused, or ``''`` when it may proceed.
 *
 * ``logout_in_progress`` and ``logout_incomplete`` are the same two states the
 * page shows; keeping the strings stable lets the local shell and the bridge
 * name the refusal without inventing a second vocabulary.
 */
export type SignOutRefusal = '' | 'logout_in_progress' | 'logout_incomplete'

export class SignOutCoordinator {
  private inFlight: Promise<SignOutOutcome> | null = null
  private failed = false

  /**
   * ``perform`` is the existing detach path (``detachRemoteContainer``): it
   * blocks the container first and revokes server-side second, so a failure is
   * a real "not confirmed" rather than a local illusion of success.
   */
  constructor(private readonly perform: () => Promise<SignOutOutcome>) {}

  /** True while a sign-out is running (the merge window). */
  get active(): boolean {
    return this.inFlight !== null
  }

  /** The stable reason the last sign-out failed, or ``''``. */
  get blockedReason(): string {
    return this.failed ? 'logout_incomplete' : ''
  }

  /** The reason a new attach must be refused, or ``''`` when allowed. */
  attachRefusal(): SignOutRefusal {
    if (this.inFlight) return 'logout_in_progress'
    return this.failed ? 'logout_incomplete' : ''
  }

  /**
   * Sign out, merging with any sign-out already running.
   *
   * A retry after a failure is allowed -- that is the "retry the sign-out"
   * affordance, and it is the only way out of the blocked state.
   */
  signOut(): Promise<SignOutOutcome> {
    if (this.inFlight) return this.inFlight
    const promise = this.runOnce()
    this.inFlight = promise
    return promise
  }

  private async runOnce(): Promise<SignOutOutcome> {
    try {
      const outcome = await this.perform()
      this.failed = !outcome.ok
      return outcome
    } catch (err) {
      // A thrown sign-out is still a sign-out that did not complete: the block
      // has to stand, and the caller has to see why.
      this.failed = true
      throw err
    } finally {
      this.inFlight = null
    }
  }
}
