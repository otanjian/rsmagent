/* Session context controls: live usage read + synchronous compaction
 * (change integrate-upstream-core-capabilities, P2).
 *
 * The module is a plain IIFE, `defer`-loaded before console.js, so it must not
 * touch the DOM or the network at load time. The pure half is tested directly;
 * the mounted half is driven against a minimal fake document so the request
 * shapes, the feature gate and the stale-response rule are exercised for real.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const MODULE_PATH = 'channel/web/static/js/functional-context.js';

function loadModule() {
    const sandbox = { window: {} };
    vm.runInNewContext(fs.readFileSync(MODULE_PATH, 'utf8'), sandbox, {
        filename: MODULE_PATH,
    });
    return { api: sandbox.window.RdaiFunctionalContext, sandbox };
}

const loaded = loadModule();
const api = loaded.api;
const sandboxWindow = loaded.sandbox.window;

// The page loads functional-capabilities.js first, so its projection reader and
// its capture/isCurrent pair are part of the environment this module is written
// against. This reduces that module's contract to what the context module uses.
function installCapabilities(overrides) {
    for (const key of Object.keys(sandboxWindow)) delete sandboxWindow[key];
    sandboxWindow.RdaiFunctionalCapabilities = Object.assign({
        available: (context, key) => Boolean(
            context && context.feature_actions && context.feature_actions[key]
            && context.feature_actions[key].available === true),
        capture: (rev, agentId, sessionId) => ({
            contextRevision: rev,
            agentId: String(agentId == null ? '' : agentId),
            sessionId: String(sessionId == null ? '' : sessionId),
        }),
        isCurrent: (captured, rev, agentId, sessionId) => Boolean(captured)
            && captured.contextRevision === rev
            && captured.agentId === String(agentId == null ? '' : agentId)
            && captured.sessionId === String(sessionId == null ? '' : sessionId),
    }, overrides || {});
}
installCapabilities();

// ---------------------------------------------------------------------------
// A minimal DOM: only what `mount` actually uses.
// ---------------------------------------------------------------------------

function makeNode(doc, tag) {
    const upper = String(tag).toUpperCase();
    const classes = new Set();
    const node = {
        ownerDocument: doc,
        tagName: upper,
        className: '',
        type: '',
        disabled: false,
        children: [],
        listeners: {},
        attributes: {},
        parentNode: null,
        classList: {
            add: (...names) => names.forEach((name) => classes.add(name)),
            remove: (...names) => names.forEach((name) => classes.delete(name)),
            contains: (name) => classes.has(name),
            toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
        },
        // Setting `textContent` in a real DOM replaces every child; the module
        // uses that to empty the panel before repopulating it.
        get textContent() {
            if (this.children.length) {
                return this.children.map((child) => child.textContent).join('');
            }
            return this._text || '';
        },
        set textContent(value) {
            this._text = value === null || value === undefined ? '' : String(value);
            for (const child of this.children) child.parentNode = null;
            this.children = [];
        },
        appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
        removeChild(child) {
            this.children = this.children.filter((item) => item !== child);
            child.parentNode = null;
            return child;
        },
        remove() {
            this.removed = true;
            if (this.parentNode) this.parentNode.removeChild(this);
        },
        addEventListener(name, handler) {
            (this.listeners[name] = this.listeners[name] || []).push(handler);
        },
        setAttribute(name, value) { this.attributes[name] = value; },
        querySelector() { return null; },
        get firstChild() { return this.children[0] || null; },
    };
    void upper;
    return node;
}

function makeDocument() {
    const doc = { body: null };
    doc.createElement = (tag) => makeNode(doc, tag);
    doc.body = makeNode(doc, 'body');
    return doc;
}

function find(root, predicate) {
    if (predicate(root)) return root;
    for (const child of root.children) {
        const hit = find(child, predicate);
        if (hit) return hit;
    }
    return null;
}

function byClass(root, token) {
    const needle = ' ' + token + ' ';
    return find(root, (node) => (' ' + node.className + ' ').indexOf(needle) !== -1);
}

function click(node) {
    for (const handler of node.listeners.click || []) handler({ target: node });
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

// The projection every action is consulted against. A key the server did not
// report open is closed, and a closed action renders nothing rather than a
// control that can only fail.
const ALL_OPEN = { feature_actions: {
    'session_context.usage': { available: true },
    'session_context.compact': { available: true },
} };

const USAGE = {
    status: 'success',
    available: true,
    estimated: true,
    model: 'm-1',
    window: 128000,
    limit: 100000,
    used: 25000,
    messages: 12,
    breakdown: { system: 5000, tools: 8000, history: 12000, free: 75000 },
};

function mountContext(responses, context, initialSession, deps) {
    const doc = makeDocument();
    const root = makeNode(doc, 'div');
    const calls = [];
    let session = initialSession
        || { agentId: 'a1', sessionId: 's1', persisted: true };
    const handle = api.mount(Object.assign({
        root,
        document: doc,
        t: (key) => key,
        getContext: () => context || ALL_OPEN,
        getContextRevision: () => 1,
        getSession: () => session,
        request: (path, options) => {
            calls.push({ path, options });
            const matched = Object.keys(responses).find((key) => path.indexOf(key) !== -1);
            const answer = matched === undefined ? null : responses[matched];
            if (answer instanceof Error) return Promise.reject(answer);
            if (typeof answer === 'function') return answer(path, options);
            return Promise.resolve(answer);
        },
    }, deps || {}));
    return {
        handle, root, calls,
        setSession: (next) => { session = next; },
        getSession: () => session,
    };
}

// ---------------------------------------------------------------------------
// Loading contract
// ---------------------------------------------------------------------------

test('the module registers exactly one namespace and does no work at load time', () => {
    // The sandbox has no `document` and no network at all: a load-time DOM or
    // fetch access would throw before the assertion, which is exactly the
    // failure this guards.
    const sandbox = { window: {} };
    vm.runInNewContext(fs.readFileSync(MODULE_PATH, 'utf8'), sandbox, {
        filename: MODULE_PATH,
    });
    assert.deepEqual(Object.keys(sandbox.window), ['RdaiFunctionalContext']);
    assert.equal(typeof sandbox.window.RdaiFunctionalContext.mount, 'function');
    assert.equal(typeof sandbox.window.RdaiFunctionalContext.buildUsageView, 'function');
    assert.equal(typeof sandbox.window.RdaiFunctionalContext.panelState, 'function');
    assert.equal(typeof sandbox.window.RdaiFunctionalContext.buildCompactRequest, 'function');
    assert.equal(typeof sandbox.window.RdaiFunctionalContext.errorKey, 'function');
});

// ---------------------------------------------------------------------------
// Pure: usage view model
// ---------------------------------------------------------------------------

test('a usage view model carries the server numbers and a clamped percent', () => {
    const view = api.buildUsageView(USAGE);
    assert.equal(view.available, true);
    assert.equal(view.estimated, true);
    assert.equal(view.used, 25000);
    assert.equal(view.limit, 100000);
    assert.equal(view.messages, 12);
    assert.equal(view.percent, 25);
    assert.deepEqual(JSON.parse(JSON.stringify(view.breakdown)),
        { system: 5000, tools: 8000, history: 12000, free: 75000 });

    // used may exceed limit (the trimmer budgets tools separately): the ring
    // must saturate rather than run past 100.
    assert.equal(api.buildUsageView({ status: 'success', available: true, used: 150, limit: 100 })
        .percent, 100);
});

test('an available:false success is a view model, never an error or a figure', () => {
    // The no-live-context answer is a successful read, so it must not be
    // rendered through the error path -- and it carries no usage to display.
    const view = api.buildUsageView({ status: 'success', available: false });
    assert.equal(view.available, false);
    assert.equal(view.used, undefined);
    assert.equal(view.limit, undefined);
    // A malformed or refused payload is not a usage view at all.
    assert.equal(api.buildUsageView(null), null);
    assert.equal(api.buildUsageView({ status: 'error', code: 'internal_error' }), null);
});

// ---------------------------------------------------------------------------
// Pure: panel state
// ---------------------------------------------------------------------------

test('the panel state separates closed, unavailable, conflict, refused, error and ready', () => {
    assert.equal(api.panelState({ actionAvailable: false }), 'closed');
    assert.equal(api.panelState({ actionAvailable: true, usage: { available: false } }), 'unavailable');
    assert.equal(api.panelState({ actionAvailable: true, error: { code: 'session_busy', status: 409 } }),
        'conflict');
    assert.equal(api.panelState({ actionAvailable: true, error: { code: 'context_changed', status: 409 } }),
        'conflict');
    assert.equal(api.panelState({ actionAvailable: true, error: { code: 'forbidden', status: 403 } }),
        'refused');
    assert.equal(api.panelState({ actionAvailable: true, error: { code: 'store_unavailable', status: 503 } }),
        'error');
    assert.equal(api.panelState({ actionAvailable: true, usage: { available: true } }), 'ready');
    assert.equal(api.panelState({ actionAvailable: true, loading: true }), 'loading');
});

test('error codes map to their own locale keys instead of one generic failure', () => {
    assert.equal(api.errorKey('store_unavailable', 503), 'context_error_store_unavailable');
    assert.equal(api.errorKey('internal_error', 500), 'context_error_internal_error');
    assert.equal(api.errorKey('invalid_request', 400), 'context_error_invalid');
    assert.equal(api.errorKey('session_not_found', 404), 'context_error_session_missing');
    assert.equal(api.errorKey('forbidden', 403), 'context_denied');
    assert.equal(api.errorKey('', 403), 'context_denied');
    // A code the vocabulary does not know still lands on a real message key,
    // never on a fabricated client-side code.
    assert.equal(api.errorKey('brand_new_code', 500), 'context_error');
});

// ---------------------------------------------------------------------------
// Pure: compaction request + outcome
// ---------------------------------------------------------------------------

test('a compaction request targets the session and omits an unknown Agent', () => {
    const withAgent = api.buildCompactRequest('s/1', 'a-1');
    assert.equal(withAgent.path, '/api/sessions/s%2F1/compact_context');
    assert.deepEqual(JSON.parse(JSON.stringify(withAgent.body)), { agent_id: 'a-1' });

    const anonymous = api.buildCompactRequest('s1', '');
    assert.deepEqual(JSON.parse(JSON.stringify(anonymous.body)), {});
    assert.throws(() => api.buildCompactRequest('', 'a-1'), (error) => error.code === 'missing_session');
});

test('a no-op compaction is a successful outcome, not a failure', () => {
    const done = api.compactOutcome({ status: 'success', ok: true, reason: 'compacted' });
    assert.equal(done.ok, true);
    assert.equal(done.noop, false);

    for (const reason of ['no_live_context', 'nothing_to_compact']) {
        const noop = api.compactOutcome({ status: 'success', ok: false, reason });
        assert.equal(noop.ok, false, reason);
        assert.equal(noop.noop, true, reason);
        assert.equal(noop.key, 'context_compact_noop', reason);
    }

    const failed = api.compactOutcome({ status: 'success', ok: false, reason: 'weird' });
    assert.equal(failed.ok, false);
    assert.equal(failed.noop, false);
});

// ---------------------------------------------------------------------------
// Mounted: feature gating
// ---------------------------------------------------------------------------

test('a closed usage action issues no request and renders the not-available state', async () => {
    installCapabilities();
    const mounted = mountContext({}, { feature_actions: {} },
        { agentId: 'a1', sessionId: 's1', persisted: true });
    await mounted.handle.refresh();

    assert.deepEqual(mounted.calls, [], 'a closed action must not be requested');
    assert.equal(byClass(mounted.root, 'rdai-context-state').textContent, 'context_not_open');
    assert.equal(mounted.handle.getState().panel, 'closed');
});

test('a closed compaction action hides the action and issues no write', async () => {
    installCapabilities();
    const mounted = mountContext({ 'context_usage': USAGE }, {
        feature_actions: { 'session_context.usage': { available: true } },
    }, { agentId: 'a1', sessionId: 's1', persisted: true });
    await mounted.handle.refresh();

    assert.equal(byClass(mounted.root, 'rdai-context-compact'), null,
        'a closed write is not offered');
    assert.equal(mounted.calls.filter((call) => call.path.indexOf('compact_context') >= 0).length, 0);
});

test('an unpersisted session is never polled', async () => {
    installCapabilities();
    const mounted = mountContext({ 'context_usage': USAGE }, ALL_OPEN,
        { agentId: 'a1', sessionId: 'fresh', persisted: false });
    await mounted.handle.refresh();
    assert.deepEqual(mounted.calls, [], 'a session with no server record has no context to read');
    assert.equal(mounted.handle.getState().panel, 'unavailable',
        'a not-yet-persisted session is reported as having no live context');
    assert.equal(byClass(mounted.root, 'rdai-context-state').textContent, 'context_empty');
});

// ---------------------------------------------------------------------------
// Mounted: usage states
// ---------------------------------------------------------------------------

test('an available:false read is not an error and not a live figure', async () => {
    installCapabilities();
    const mounted = mountContext({ 'context_usage': { status: 'success', available: false } },
        ALL_OPEN, { agentId: 'a1', sessionId: 's1', persisted: true });
    await mounted.handle.refresh();

    const state = mounted.handle.getState();
    assert.equal(state.usage.available, false);
    assert.equal(state.panel, 'unavailable');
    assert.equal(byClass(mounted.root, 'rdai-context-state').textContent, 'context_empty');
    assert.equal(byClass(mounted.root, 'rdai-context-figure').classList.contains('hidden'), true,
        'no figure is drawn for a session with no live context');
    assert.equal(byClass(mounted.root, 'rdai-context-error').textContent, '');
});

test('a live read renders the figures and the estimated marker', async () => {
    installCapabilities();
    const mounted = mountContext({ 'context_usage': USAGE }, ALL_OPEN,
        { agentId: 'a1', sessionId: 's1', persisted: true });
    await mounted.handle.refresh();

    assert.equal(mounted.handle.getState().panel, 'ready');
    const figure = byClass(mounted.root, 'rdai-context-figure');
    assert.equal(figure.classList.contains('hidden'), false);
    assert.match(figure.textContent, /context_used_of/);
    assert.match(figure.textContent, /context_estimated/);
});

test('a 503 and a 500 keep the server code instead of a generic failure', async () => {
    for (const [code, status, key] of [
        ['store_unavailable', 503, 'context_error_store_unavailable'],
        ['internal_error', 500, 'context_error_internal_error'],
    ]) {
        installCapabilities();
        const mounted = mountContext({
            'context_usage': () => Promise.reject(Object.assign(new Error('boom'), { code, status })),
        }, ALL_OPEN, { agentId: 'a1', sessionId: 's1', persisted: true });
        await mounted.handle.refresh();
        const state = mounted.handle.getState();
        assert.equal(state.error.code, code);
        assert.equal(state.panel, 'error');
        assert.equal(byClass(mounted.root, 'rdai-context-state').textContent, key);
    }
});

test('a stale usage response after a session switch never overwrites the newer one', async () => {
    installCapabilities();
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const mounted = mountContext({
        '/api/sessions/s1': () => gate,
        '/api/sessions/s2': { status: 'success', available: true, used: 10, limit: 100 },
    }, ALL_OPEN, { agentId: 'a1', sessionId: 's1', persisted: true });

    void mounted.handle.refresh();          // s1 in flight
    mounted.setSession({ agentId: 'a1', sessionId: 's2', persisted: true });
    release({ status: 'success', available: true, used: 99, limit: 100 }); // old answer lands
    await tick();
    await tick();
    assert.equal(mounted.handle.getState().usage, null,
        'an answer for the session that was left must not be applied');

    await mounted.handle.refresh();         // the new session's own read
    assert.equal(mounted.handle.getState().usage.used, 10);
});

// ---------------------------------------------------------------------------
// Mounted: compaction
// ---------------------------------------------------------------------------

test('a no_live_context compaction reads as a successful no-op', async () => {
    installCapabilities();
    const mounted = mountContext({
        'context_usage': USAGE,
        '/compact_context': {
            status: 'success', ok: false, available: false,
            reason: 'no_live_context', usage: null,
        },
    }, ALL_OPEN, { agentId: 'a1', sessionId: 's1', persisted: true });
    await mounted.handle.refresh();

    click(byClass(mounted.root, 'rdai-context-compact'));
    await tick();
    await tick();

    assert.equal(mounted.handle.getState().compactState, 'noop');
    assert.equal(byClass(mounted.root, 'rdai-context-notice').textContent, 'context_compact_noop');
    assert.equal(byClass(mounted.root, 'rdai-context-error').textContent, '');
});

test('session_busy and context_changed map to their own states', async () => {
    for (const [code, key] of [
        ['session_busy', 'context_compact_busy'],
        ['context_changed', 'context_compact_changed'],
    ]) {
        installCapabilities();
        const mounted = mountContext({
            'context_usage': USAGE,
            '/compact_context': () => Promise.reject(
                Object.assign(new Error('conflict'), { code, status: 409 })),
        }, ALL_OPEN, { agentId: 'a1', sessionId: 's1', persisted: true });
        await mounted.handle.refresh();

        click(byClass(mounted.root, 'rdai-context-compact'));
        await tick();
        await tick();

        assert.equal(mounted.handle.getState().compactState, 'conflict', code);
        assert.equal(byClass(mounted.root, 'rdai-context-error').textContent, key, code);
        assert.equal(mounted.handle.getState().noticeKey, '');
    }
});

test('a successful compaction surfaces its completion and refreshes the usage', async () => {
    installCapabilities();
    const mounted = mountContext({
        'context_usage': USAGE,
        '/compact_context': {
            status: 'success', ok: true, available: true, reason: 'compacted',
            compacted_turns: 4, before: 12, after: 8,
            usage: { status: 'success', available: true, used: 5000, limit: 100000 },
        },
    }, ALL_OPEN, { agentId: 'a1', sessionId: 's1', persisted: true });
    await mounted.handle.refresh();

    click(byClass(mounted.root, 'rdai-context-compact'));
    await tick();
    await tick();

    assert.equal(mounted.handle.getState().compactState, 'ready');
    assert.equal(byClass(mounted.root, 'rdai-context-notice').textContent, 'context_compact_done');
    assert.equal(mounted.handle.getState().usage.used, 5000);
});

test('one click fires exactly one compaction request', async () => {
    installCapabilities();
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    let compactions = 0;
    const mounted = mountContext({
        'context_usage': USAGE,
        '/compact_context': () => { compactions += 1; return gate; },
    }, ALL_OPEN, { agentId: 'a1', sessionId: 's1', persisted: true });
    await mounted.handle.refresh();

    const button = byClass(mounted.root, 'rdai-context-compact');
    click(button);
    click(button); // a second click while the write is in flight is ignored
    await tick();
    assert.equal(compactions, 1, 'the write is not retried by a double click');

    release({ status: 'success', ok: true, reason: 'compacted', usage: null });
    await tick();
    await tick();
    assert.equal(compactions, 1);
});

test('an injected confirmation gates the write until it is accepted', async () => {
    installCapabilities();
    let pending = null;
    let compactions = 0;
    const mounted = mountContext({
        'context_usage': USAGE,
        '/compact_context': () => {
            compactions += 1;
            return Promise.resolve({ status: 'success', ok: true, reason: 'compacted', usage: null });
        },
    }, ALL_OPEN, { agentId: 'a1', sessionId: 's1', persisted: true }, {
        confirm: (options) => { pending = options; },
    });
    await mounted.handle.refresh();

    click(byClass(mounted.root, 'rdai-context-compact'));
    assert.ok(pending, 'the confirmation is requested before any write');
    assert.equal(compactions, 0);
    assert.equal(mounted.handle.getState().compacting, true);

    pending.onCancel();
    await tick();
    assert.equal(compactions, 0);
    assert.equal(mounted.handle.getState().compacting, false);

    click(byClass(mounted.root, 'rdai-context-compact'));
    pending.onConfirm();
    await tick();
    await tick();
    assert.equal(compactions, 1);
});

test('dispose stops a response that lands afterwards', async () => {
    installCapabilities();
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const mounted = mountContext({ 'context_usage': () => gate }, ALL_OPEN,
        { agentId: 'a1', sessionId: 's1', persisted: true });
    const pending = mounted.handle.refresh();
    mounted.handle.dispose();
    release({ status: 'success', available: true, used: 42, limit: 100 });
    await pending;
    assert.equal(mounted.root.children.length, 0, 'the detached tree is emptied');
});
