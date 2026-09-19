/* Client-side half of the action-projection contract
 * (change integrate-upstream-core-capabilities, tasks 2.3/2.5).
 *
 * The Web module loads in a bare VM context: `functional-capabilities.js` is a
 * plain IIFE that must not touch the DOM at load time (it is `defer`-loaded
 * before console.js, so the shell's nodes do not exist yet).
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const MODULE_PATH = 'channel/web/static/js/functional-capabilities.js';

function loadModule() {
    const sandbox = { window: {} };
    vm.runInNewContext(fs.readFileSync(MODULE_PATH, 'utf8'), sandbox, {
        filename: MODULE_PATH
    });
    return sandbox.window.RdaiFunctionalCapabilities;
}

const api = loadModule();

function contextWith(actions) {
    return { status: 'success', feature_actions: actions };
}

test('exposes exactly the eight declared keys in server order', () => {
    // Round-tripped through JSON: the module's array lives in another VM realm,
    // so its prototype is not this realm's Array.prototype and deepStrictEqual
    // would reject structurally-equal data.
    assert.deepEqual(JSON.parse(JSON.stringify(api.KEYS)), [
        'session_context.usage',
        'session_context.compact',
        'scheduler.instances',
        'scheduler.recipients',
        'scheduler.create',
        'scheduler.runs.list',
        'scheduler.runs.detail',
        'scheduler.runs.delete'
    ]);
});

test('all eight keys are available when the server reports them open', () => {
    const actions = {};
    for (const key of api.KEYS) actions[key] = { available: true, reason: '' };
    const context = contextWith(actions);
    for (const key of api.KEYS) {
        assert.equal(api.available(context, key), true, key);
    }
});

test('a closed action is never reported available', () => {
    const context = contextWith({
        'session_context.usage': { available: false, reason: 'not_accepted' }
    });
    assert.equal(api.available(context, 'session_context.usage'), false);
    assert.equal(api.reason(context, 'session_context.usage'), 'not_accepted');
});

test('a missing feature_actions field closes every new capability', () => {
    // An old server predating the projection: the client must not guess.
    const legacy = { status: 'success', consumers: {}, console_pages: {} };
    for (const key of api.KEYS) {
        assert.equal(api.available(legacy, key), false, key);
        assert.equal(api.reason(legacy, key), 'unknown', key);
    }
});

test('a missing single key closes only that action', () => {
    const context = contextWith({
        'session_context.compact': { available: true, reason: '' }
    });
    assert.equal(api.available(context, 'session_context.compact'), true);
    assert.equal(api.available(context, 'session_context.usage'), false);
});

test('a non-success context closes every action', () => {
    const context = { status: 'error', feature_actions: {} };
    assert.equal(api.available(context, 'session_context.usage'), false);
    assert.equal(api.available(null, 'session_context.usage'), false);
    assert.equal(api.available(undefined, 'session_context.usage'), false);
});

test('a truthy non-boolean available does not open the gate', () => {
    // A malformed server response must fail closed: 'yes' / 1 / {} are not true.
    for (const value of ['yes', 1, {}, [], 'true']) {
        const context = contextWith({ 'scheduler.create': { available: value } });
        assert.equal(api.available(context, 'scheduler.create'), false,
            JSON.stringify(value));
    }
});

test('all() resolves every key and keeps the reason distinct', () => {
    const context = contextWith({
        'scheduler.runs.delete': { available: false, reason: 'disabled_by_deployment' }
    });
    const resolved = api.all(context);
    assert.equal(Object.keys(resolved).length, 8);
    assert.equal(resolved['scheduler.runs.delete'].reason, 'disabled_by_deployment');
    assert.equal(resolved['scheduler.runs.list'].available, false);
});

test('capture/isCurrent refuse a late response after any identity switch', () => {
    const token = api.capture(4, 'agent-a', 'session-1');
    assert.equal(api.isCurrent(token, 4, 'agent-a', 'session-1'), true);
    // Revision moved: logout, tenant switch or reconnect invalidated the view.
    assert.equal(api.isCurrent(token, 5, 'agent-a', 'session-1'), false);
    // Agent or session switched under the same revision.
    assert.equal(api.isCurrent(token, 4, 'agent-b', 'session-1'), false);
    assert.equal(api.isCurrent(token, 4, 'agent-a', 'session-2'), false);
});

test('isCurrent tolerates null/undefined identity inputs', () => {
    const token = api.capture(0, null, undefined);
    assert.equal(api.isCurrent(token, 0, '', ''), true);
    assert.equal(api.isCurrent(null, 0, '', ''), false);
});

test('the module attaches exactly one global and needs no DOM', () => {
    const sandbox = { window: {} };
    vm.runInNewContext(fs.readFileSync(MODULE_PATH, 'utf8'), sandbox, {
        filename: MODULE_PATH
    });
    assert.deepEqual(Object.keys(sandbox.window), ['RdaiFunctionalCapabilities']);
    // No document was supplied, so a load-time DOM access would have thrown.
    assert.equal(typeof sandbox.window.RdaiFunctionalCapabilities.available, 'function');
});
