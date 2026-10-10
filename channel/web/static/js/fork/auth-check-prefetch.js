// The console's bounded, one-shot /auth/check request primitive
// (change optimize-console-login-startup, design D2/D3).
//
// The console used to learn who the caller was only from deep inside
// `console.js`: the page first downloaded the whole bundle, and only then did
// `refreshAccountIdentity()` issue the check. This module is loaded from <head>
// ahead of the bundle, so the read-only check overlaps the download instead of
// queueing behind it.
//
// It owns exactly one thing -- the fixed endpoint's request, body read, cancel
// and 5s deadline -- and exposes:
//   * `takeStartup()`: the eagerly-started result, consumed at most once;
//   * `request()`: a fresh bounded check for manual retry / no prefetch;
//   * `discardStartup()`: drop and cancel an unconsumed startup check.
// It never reads the login form, writes the DOM, persists an identity, or
// rewrites `fetch`; the console's own account state machine still decides what
// a result means and remains the only owner of the account UI.
(function () {
    'use strict';

    // From issue to a fully-read response body. A check that has not finished
    // by then settles as a timeout, so the UI can offer a retry instead of
    // waiting on a hung request forever.
    var DEADLINE_MS = 5000;
    // A startup result is only reused while it is this recent. It matches the
    // deadline, so a check that outlived its own deadline is never reused.
    var STARTUP_MAX_AGE_MS = 5000;

    // Start one bounded check. The returned promise ALWAYS resolves to a
    // captured outcome -- it never rejects -- so a failed startup check cannot
    // surface as an unhandled rejection before the consumer has even loaded.
    // Outcomes: { kind: 'response', status, data, validBody }
    //           { kind: 'timeout' } | { kind: 'error', error }
    //           { kind: 'cancelled' }
    function boundedCheck() {
        var controller = typeof AbortController === 'function' ? new AbortController() : null;
        var settled = false;
        var timer = null;
        var finish = null;
        var outcome = new Promise(function (resolve) { finish = resolve; });

        function abort() {
            if (!controller) return;
            try { controller.abort(); } catch (_) { /* already gone */ }
        }

        // The single settle point: every path that ends a check goes through it,
        // so success, failure, timeout and cancel all release the timer.
        function settle(value) {
            if (settled) return;
            settled = true;
            if (timer !== null && typeof clearTimeout === 'function') clearTimeout(timer);
            finish(value);
        }

        timer = setTimeout(function () {
            abort();
            settle({ kind: 'timeout' });
        }, DEADLINE_MS);

        var options = { credentials: 'same-origin', cache: 'no-store' };
        if (controller) options.signal = controller.signal;

        Promise.resolve().then(function () {
            return fetch('/auth/check', options);
        }).then(function (response) {
            if (settled) return null;
            // Reading the body is part of the same deadline. An unreadable body
            // is not an unauthenticated caller: preserve the HTTP status and
            // mark the body invalid rather than guessing a conclusion.
            return Promise.resolve().then(function () { return response.json(); }).then(
                function (data) { return { data: data, validBody: true }; },
                function () { return { data: null, validBody: false }; }
            ).then(function (parsed) {
                if (settled) return;
                settle({ kind: 'response', status: response.status,
                    data: parsed.data, validBody: parsed.validBody });
            });
        }, function (error) {
            // A late rejection after the deadline was already settled is
            // ignored here; the timeout result stands.
            if (settled) return;
            settle({ kind: 'error', error: error });
        });

        return {
            outcome: outcome,
            cancel: function () { abort(); settle({ kind: 'cancelled' }); },
        };
    }

    var startup = boundedCheck();
    var startupIssuedAt = Date.now();
    var startupConsumed = false;

    window.__cowAccountCheck__ = {
        DEADLINE_MS: DEADLINE_MS,
        STARTUP_MAX_AGE_MS: STARTUP_MAX_AGE_MS,
        request: boundedCheck,
        takeStartup: function () {
            if (startupConsumed) return null;
            startupConsumed = true;
            return { issuedAt: startupIssuedAt, outcome: startup.outcome, cancel: startup.cancel };
        },
        discardStartup: function () {
            if (startupConsumed) return;
            startupConsumed = true;
            startup.cancel();
        },
    };
}());
