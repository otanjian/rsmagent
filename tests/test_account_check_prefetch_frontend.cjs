// Execute the shipped bounded /auth/check primitive (change
// optimize-console-login-startup, tasks 2.1 / 3.1) with controlled requests.
//
// The login console used to issue its first account check from deep inside the
// 1.1 MB console bundle, after the whole page had downloaded. This module runs
// from <head> and issues that one read-only check immediately, then hands the
// captured outcome to `console.js` for at most one reuse.
//
// These tests pin the primitive's own contract: a bounded, non-rejecting,
// one-shot startup result and a reusable fresh check for manual retries.
//
// Run: node --test tests/test_account_check_prefetch_frontend.cjs

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.join(__dirname, '../channel/web/static/js/fork/auth-check-prefetch.js'), 'utf8');

const settle = () => new Promise(resolve => setImmediate(resolve));
const response = (data, status = 200) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
});

/**
 * Load the module with a manual timer queue so the 5s deadline is driven by
 * the test instead of wall-clock time.
 */
function load(fetchImpl) {
    const timers = new Map();
    let nextId = 1;
    const fetched = [];
    const ctx = {
        console, Date, Promise, JSON, Math, AbortController,
        setTimeout(fn, ms) { const id = nextId++; timers.set(id, { fn, ms }); return id; },
        clearTimeout(id) { timers.delete(id); },
        fetch(url, options) {
            fetched.push({ url, options });
            return Promise.resolve().then(() => fetchImpl(url, options));
        },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(source, ctx);
    return {
        module: ctx.window.__cowAccountCheck__,
        fetched, timers,
        pendingTimers: () => timers.size,
        fireTimers: () => { const due = [...timers.values()]; timers.clear(); due.forEach(t => t.fn()); },
    };
}

const database = (username = 'alice') => ({
    status: 'success', identity_mode: 'database', auth_required: true,
    authenticated: true, user: { username, display_name: username.toUpperCase() },
});

test('the startup check is issued eagerly, before any consumer exists', async () => {
    const h = load(async () => response(database()));
    assert.equal(h.fetched.length, 0, 'the request is scheduled, not issued synchronously');
    await settle();
    assert.equal(h.fetched.length, 1);
    assert.equal(h.fetched[0].url, '/auth/check');
    assert.equal(h.fetched[0].options.credentials, 'same-origin');
    assert.equal(h.fetched[0].options.cache, 'no-store');
    assert.equal(h.fetched[0].options.headers, undefined, 'no tenant header is attached');
});

test('the startup handle is one-shot and carries the captured HTTP result', async () => {
    const h = load(async () => response(database('bob'), 200));
    await settle();
    const handle = h.module.takeStartup();
    assert.ok(handle, 'the first consumption yields the eager result');
    assert.equal(typeof handle.issuedAt, 'number');
    const outcome = await handle.outcome;
    assert.equal(outcome.kind, 'response');
    assert.equal(outcome.status, 200);
    assert.equal(outcome.validBody, true);
    assert.equal(outcome.data.user.username, 'bob');
    assert.equal(h.module.takeStartup(), null, 'a second consumption must not get the startup result');
});

test('a fresh bounded request is available for manual retries and does not reuse startup', async () => {
    const h = load(async () => response(database('alice')));
    await settle();
    h.module.takeStartup();  // consume the startup result
    const retried = h.module.request();
    await settle();
    assert.equal(h.fetched.length, 2, 'the retry issues its own request');
    const outcome = await retried.outcome;
    assert.equal(outcome.kind, 'response');
    assert.equal(outcome.data.user.username, 'alice');
});

test('HTTP status is preserved and an unreadable body is flagged, never swallowed as success', async () => {
    for (const [status, payload] of [[401, { status: 'error' }], [503, { status: 'error' }]]) {
        const h = load(async () => response(payload, status));
        await settle();
        const outcome = await h.module.takeStartup().outcome;
        assert.equal(outcome.kind, 'response');
        assert.equal(outcome.status, status, `HTTP ${status} must survive the request layer`);
        assert.equal(outcome.validBody, true);
    }
    const broken = load(async () => ({ ok: true, status: 200, json: async () => { throw Error('not json'); } }));
    await settle();
    const outcome = await broken.module.takeStartup().outcome;
    assert.equal(outcome.kind, 'response');
    assert.equal(outcome.status, 200);
    assert.equal(outcome.validBody, false, 'an unparseable body is not a valid authentication response');
    assert.equal(outcome.data, null);
});

test('a network failure is captured as a result, so no unhandled rejection fires', async () => {
    const h = load(async () => { throw Error('connection lost'); });
    await settle();
    const outcome = await h.module.takeStartup().outcome;
    assert.equal(outcome.kind, 'error');
    assert.match(outcome.error.message, /connection lost/);
});

test('a hung response body is ended by the 5s deadline as a timeout', async () => {
    const pending = { ok: true, status: 200, json: () => new Promise(() => {}) };
    const h = load(async () => pending);
    await settle();
    assert.equal(h.module.DEADLINE_MS, 5000);
    assert.equal(h.pendingTimers(), 1, 'the deadline is armed while the body is unread');
    h.fireTimers();
    const outcome = await h.module.takeStartup().outcome;
    assert.equal(outcome.kind, 'timeout');
    assert.equal(h.pendingTimers(), 0, 'settling clears the deadline timer');
});

test('a request whose headers never arrive is bounded by the deadline and aborted', async () => {
    let signal = null;
    const h = load(async (url, options) => {
        signal = options.signal;
        return new Promise(() => {});
    });
    await settle();
    assert.ok(signal, 'the request carries an abort signal');
    assert.equal(signal.aborted, false);
    h.fireTimers();
    const outcome = await h.module.takeStartup().outcome;
    assert.equal(outcome.kind, 'timeout');
    assert.equal(signal.aborted, true, 'the deadline aborts the in-flight request');
});

test('a completed check clears its timer', async () => {
    const h = load(async () => response(database()));
    await settle();
    await h.module.takeStartup().outcome;
    assert.equal(h.pendingTimers(), 0);
});

test('discarding an unconsumed startup check cancels it and it can no longer be taken', async () => {
    let abortSignal = null;
    const h = load(async (url, options) => { abortSignal = options.signal; return new Promise(() => {}); });
    await settle();
    h.module.discardStartup();
    assert.equal(h.module.takeStartup(), null, 'a discarded startup result is gone');
    assert.equal(abortSignal.aborted, true, 'discarding cancels the in-flight check');
    assert.equal(h.pendingTimers(), 0);
});

test('discarding an already-consumed startup result is a no-op', async () => {
    const h = load(async () => response(database()));
    await settle();
    const handle = h.module.takeStartup();
    h.module.discardStartup();
    const outcome = await handle.outcome;
    assert.equal(outcome.kind, 'response', 'the consumed result keeps flowing to its consumer');
});
