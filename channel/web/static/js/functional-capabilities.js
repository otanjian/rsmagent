/* functional-capabilities.js - the shared client gate for the eight
 * per-action features delivered by the change
 * `integrate-upstream-core-capabilities` (design D2, implementation.md §3).
 *
 * Why a module instead of inline checks
 * -------------------------------------
 * The availability of a feature is *service* state, not caller privilege: the
 * server projects it through `/auth/context.feature_actions`, one entry per
 * action, and every consumer of this file must read that same declaration. The
 * console used to infer "an entry point exists, so the feature is usable",
 * which is exactly the drift the server-side registry now prevents -- a closed
 * action answers 503 at the gate, and a UI that guessed from the menu would
 * merely fail late and noisily.
 *
 * The second job is staleness. A response that started before a logout, a
 * tenant switch or a reconnect must never repaint the new identity's screen
 * (the "late response revives a stale tenant" bug). `capture()` records the
 * version triple the request began under and `isCurrent()` refuses to apply a
 * result whose triple has moved on.
 */
(function () {
    'use strict';

    // The eight public keys, in the server's own order. Kept here so a client
    // can iterate without hard-coding the list at each call site, and so the
    // parity test can compare it with `auth/capability_matrix.FEATURE_ACTIONS`.
    var KEYS = [
        'session_context.usage',
        'session_context.compact',
        'scheduler.instances',
        'scheduler.recipients',
        'scheduler.create',
        'scheduler.runs.list',
        'scheduler.runs.detail',
        'scheduler.runs.delete'
    ];

    // Reasons the server may report. `''` is the only value that co-occurs with
    // available=true; the rest are deliberately distinct so a UI can tell
    // "not built yet" from "built but not accepted" from "turned off here".
    var REASONS = {
        NOT_IMPLEMENTED: 'not_implemented',
        NOT_ACCEPTED: 'not_accepted',
        DISABLED_BY_DEPLOYMENT: 'disabled_by_deployment'
    };

    // A context shaped like GET /auth/context. A missing field is "unknown",
    // never "available": an old server that predates feature_actions must
    // leave every new capability closed (database-runtime-consumers: "旧服务器
    // 没有新字段").
    function _entry(context, key) {
        if (!context || context.status !== 'success') return null;
        var actions = context.feature_actions;
        if (!actions || typeof actions !== 'object') return null;
        var entry = actions[key];
        if (!entry || typeof entry !== 'object') return null;
        return entry;
    }

    // The one availability predicate. Deliberately strict (`=== true`): a
    // truthy string or 1 from a malformed server response must not open a gate.
    function available(context, key) {
        var entry = _entry(context, key);
        return !!(entry && entry.available === true);
    }

    // Why it is closed, or '' when it is open. Unknown context reports the
    // generic "not available" so callers can render a single closed state.
    function reason(context, key) {
        var entry = _entry(context, key);
        if (!entry) return 'unknown';
        if (entry.available === true) return '';
        return typeof entry.reason === 'string' ? entry.reason : 'unknown';
    }

    // Every declared action resolved against one context, for a page that wants
    // to render all of its entry points in one pass.
    function all(context) {
        var out = {};
        for (var i = 0; i < KEYS.length; i++) {
            out[KEYS[i]] = {
                available: available(context, KEYS[i]),
                reason: reason(context, KEYS[i])
            };
        }
        return out;
    }

    // Record the identity a request starts under. `contextRevision` is the
    // caller's own monotonic counter (console.js uses `_authContextSeq`), so
    // this module does not need to know how the app detects a switch.
    function capture(contextRevision, agentId, sessionId) {
        return {
            contextRevision: contextRevision == null ? 0 : contextRevision,
            agentId: agentId == null ? '' : String(agentId),
            sessionId: sessionId == null ? '' : String(sessionId)
        };
    }

    // May the captured request's result still be applied? Every field must be
    // unchanged: a revise of any one of identity/tenant/agent/session makes the
    // in-flight answer belong to a screen that no longer exists.
    function isCurrent(captured, contextRevision, agentId, sessionId) {
        if (!captured) return false;
        var rev = contextRevision == null ? 0 : contextRevision;
        if (captured.contextRevision !== rev) return false;
        if (captured.agentId !== (agentId == null ? '' : String(agentId))) return false;
        if (captured.sessionId !== (sessionId == null ? '' : String(sessionId))) return false;
        return true;
    }

    window.RdaiFunctionalCapabilities = {
        KEYS: KEYS,
        REASONS: REASONS,
        available: available,
        reason: reason,
        all: all,
        capture: capture,
        isCurrent: isCurrent
    };
})();
