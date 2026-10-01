/*
 * The desktop account adapter for the console.
 *
 * Change fix-desktop-relogin-session-sync, task 2.3.
 *
 * Signing out in a container is two ends, not one. The Web session is the
 * server's, and the native session is the host's; a sign-out that ends only the
 * first leaves a page that looks signed in over a host that has no session, so
 * the next local directory bind answers ``not signed in``. The order is not a
 * preference either: the Web end must be *confirmed* ended before the host is
 * asked to finish, because a container torn down around a still-live Web
 * session would be a sign-out that did not happen.
 *
 * What this module is for, and what it deliberately is not:
 *
 *   * it holds the *serial* half -- Web end, then host end -- in one place, so
 *     no page has to remember the order;
 *   * the page keeps its own rendering and its own epoch checks: the caller
 *     supplies ``webLogout`` and reads the reply, this module never touches the
 *     DOM;
 *   * it never reloads. A reload into the ordinary password form is exactly the
 *     split state this exists to remove; a container returns to the native
 *     shell's own login, which only the host can draw;
 *   * in a browser every answer is an honest refusal, so a page needs no
 *     ``if (desktop)`` of its own.
 *
 * Loaded after ``desktop-host.js`` and before ``console.js``.
 */
(function (global) {
  'use strict';

  /** The validated host namespace, or null. The raw preload object is never read here. */
  function host() {
    var candidate = global.CowDesktopHost;
    return (candidate && typeof candidate.isDesktop === 'function') ? candidate : null;
  }

  /** True only in a container with a validated native bridge. */
  function isDesktop() {
    var candidate = host();
    return !!(candidate && candidate.isDesktop());
  }

  /**
   * Whether the account can be ended here and now.
   *
   * False in a browser *and* in a container whose build predates ``signOut``:
   * the caller shows the upgrade prompt for the second rather than degrading to
   * a Web-only login.
   */
  function canSignOut() {
    var candidate = host();
    if (!candidate || typeof candidate.canSignOut !== 'function' || !candidate.isDesktop()) {
      return Promise.resolve(false);
    }
    return Promise.resolve(candidate.canSignOut()).catch(function () { return false; });
  }

  /**
   * The Web end, when the caller does not bring its own.
   *
   * ``/auth/logout`` answering 401 means the session is *already* gone, which is
   * the outcome being asked for -- a retry after a half-finished sign-out must
   * be able to proceed rather than reporting a failure that is really "done".
   */
  function defaultWebLogout() {
    return global.fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' })
      .then(function (response) {
        return response.json().then(function (data) {
          var ended = (response.ok && data && data.status === 'success') || response.status === 401;
          return { ok: ended, status: response.status };
        }, function () {
          return { ok: response.status === 401, status: response.status };
        });
      });
  }

  /**
   * End the account: confirm the Web session, then let the host finish.
   *
   * Resolves -- never rejects -- with ``{ok: true, signedOut, revoked}`` or a
   * refusal carrying a stable ``code``: ``no_host`` (a browser), ``host_outdated``
   * (an old desktop build, detected *before* anything is ended), ``logout_incomplete``
   * (the Web session, or the host, did not confirm). The caller decides what to
   * render and, on a refusal, may offer another attempt.
   */
  function logout(options) {
    var opts = options || {};
    var webLogout = typeof opts.webLogout === 'function' ? opts.webLogout : defaultWebLogout;
    if (!isDesktop()) {
      return Promise.resolve({ ok: false, code: 'no_host', message: 'this environment has no desktop session to end' });
    }
    var candidate = host();
    // Probe first, sign out second. If the build is too old to end its own
    // session, ending only the Web half would leave exactly the split state this
    // module exists to remove -- so nothing is ended and the caller is told why.
    return Promise.resolve(candidate.canSignOut ? candidate.canSignOut() : false)
      .then(function (supported) {
        if (!supported) {
          return { ok: false, code: 'host_outdated', message: 'this desktop build cannot end the session' };
        }
        return Promise.resolve().then(function () {
          return webLogout();
        }).then(function (reply) {
          if (!reply || reply.ok !== true) {
            return { ok: false, code: 'logout_incomplete', message: 'the Web session was not confirmed ended' };
          }
          return candidate.signOut();
        });
      });
  }

  global.CowDesktopAccount = {
    isDesktop: isDesktop,
    canSignOut: canSignOut,
    logout: logout
  };
})(typeof window !== 'undefined' ? window : this);
