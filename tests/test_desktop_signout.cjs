// Desktop container sign-out: the narrow host action and its single-flight.
//
// Change ``fix-desktop-relogin-session-sync``, tasks 1.2 / 2.1. The regression
// this guards: signing out of the desktop workbench revoked the native session
// and its paired Web session, but the container then came back as an *ordinary*
// Web password login, so the page looked signed in while the host had no native
// session and every directory bind answered ``not signed in``. The fix is one
// narrow host action, ``signOut``, that ends the account through the existing
// detach/logout path instead of reloading into a plain Web form.
//
// Two things are driven for real here: the bridge's own allow-list/shape check
// (so the method cannot be added without the contract, and carries no
// parameter a page could aim), and the coordinator that merges repeated
// sign-outs and keeps the workbench from reconnecting while one is unresolved.
//
// Run: node --test tests/test_desktop_signout.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const contractPath = path.join(root, 'contracts', 'desktop', 'v1.json');
const dist = path.join(desktop, 'dist', 'main');
const compiledHostBridge = path.join(dist, 'remote', 'host-bridge.js');
const compiledSignout = path.join(dist, 'remote', 'session-signout.js');
const compiledPreload = path.join(dist, 'remote-preload.js');
// The *local shell's* preload: a different document and a different namespace
// (`electronAPI`), and the one that has to receive the sign-out notice.
const compiledShellPreload = path.join(dist, 'preload.js');

let hostBridge;
let signout;
let contract;

function needsBuild() {
    const srcDir = path.join(desktop, 'src', 'main', 'remote');
    const sources = [path.join(srcDir, 'host-bridge.ts'), path.join(srcDir, 'session-signout.ts'),
        path.join(desktop, 'src', 'main', 'remote-preload.ts'),
        path.join(desktop, 'src', 'main', 'preload.ts')];
    const newest = Math.max(...sources.map((file) => fs.statSync(file).mtimeMs));
    for (const out of [compiledHostBridge, compiledSignout, compiledPreload, compiledShellPreload]) {
        if (!fs.existsSync(out)) return true;
        if (newest > fs.statSync(out).mtimeMs) return true;
    }
    return false;
}

before(() => {
    if (needsBuild()) {
        execFileSync('npm', ['run', 'build:main'], { cwd: desktop, stdio: 'inherit' });
    }
    hostBridge = require(compiledHostBridge);
    signout = require(compiledSignout);
    contract = JSON.parse(fs.readFileSync(contractPath, 'utf8'));
});

// ---------------------------------------------------------------------------
// 1.2 the narrow host action
// ---------------------------------------------------------------------------

test('signOut is a phase-1 bridge method the contract declares', () => {
    assert.ok(hostBridge.PHASE1_METHODS.includes('signOut'),
        'a closed local-files deployment must still be able to sign out');
    assert.ok(hostBridge.ALL_BRIDGE_METHODS.includes('signOut'));
    assert.ok(contract.bridge.phase1_methods.includes('signOut'),
        'the contract and the client must agree on the phase-1 surface');
    assert.ok(contract.bridge.methods.includes('signOut'));
    // It is not a capability the bridge must never expose.
    assert.equal(contract.bridge.forbidden_capabilities.includes('signOut'), false);
});

test('signOut carries no parameter a page could aim at another account', () => {
    assert.deepEqual(hostBridge.checkBridgeCall({ method: 'signOut' }), { ok: true });
    // Extra fields are ignored rather than trusted: there is no target, token
    // or account in this action's vocabulary.
    assert.deepEqual(hostBridge.checkBridgeCall({
        method: 'signOut',
        params: { account: 'someone-else', token: 'x' },
    }), { ok: true });
});

test('the preload publishes signOut and sends exactly that method', async () => {
    const exposed = {};
    const invoked = [];
    const fakeElectron = {
        contextBridge: { exposeInMainWorld: (name, api) => { exposed[name] = api; } },
        ipcRenderer: {
            invoke: (channel, payload) => {
                invoked.push({ channel, payload });
                if (channel === 'desktop:bridge:hello') return Promise.resolve({ ok: true, generation: 3 });
                return Promise.resolve({ ok: true, data: { signedOut: true } });
            },
            on: () => undefined,
            removeListener: () => undefined,
        },
    };
    const originalRequire = Module.prototype.require;
    Module.prototype.require = function patched(id) {
        if (id === 'electron') return fakeElectron;
        return originalRequire.apply(this, arguments);
    };
    try {
        const mod = new Module(compiledPreload, null);
        mod.filename = compiledPreload;
        mod.paths = Module._nodeModulePaths(path.dirname(compiledPreload));
        mod._compile(fs.readFileSync(compiledPreload, 'utf8'), compiledPreload);
    } finally {
        Module.prototype.require = originalRequire;
    }
    const api = exposed.desktopHost;
    assert.equal(typeof api.signOut, 'function');
    assert.deepEqual(await api.signOut(), { signedOut: true });
    const call = invoked.find((entry) => entry.channel === 'desktop:bridge:call');
    assert.equal(call.payload.method, 'signOut');
    assert.equal(call.payload.generation, 3, 'the handshake generation still stamps the call');
});

// ---------------------------------------------------------------------------
// 2.2 the notice the local shell receives
// ---------------------------------------------------------------------------
//
// The container document that asked to sign out is destroyed by the detach, so
// the *end* of the sign-out cannot be delivered to it. The local shell is told
// instead -- phase and outcome only, never an account or a token -- and the
// preload is the only place that turns the channel into a subscription.

/** Load the local shell's compiled preload and drive one listener. */
function loadPreload(ipcRenderer) {
    const exposed = {};
    const fakeElectron = {
        contextBridge: { exposeInMainWorld: (name, api) => { exposed[name] = api; } },
        ipcRenderer,
    };
    const originalRequire = Module.prototype.require;
    Module.prototype.require = function patched(id) {
        if (id === 'electron') return fakeElectron;
        return originalRequire.apply(this, arguments);
    };
    try {
        const mod = new Module(compiledShellPreload, null);
        mod.filename = compiledShellPreload;
        mod.paths = Module._nodeModulePaths(path.dirname(compiledShellPreload));
        mod._compile(fs.readFileSync(compiledShellPreload, 'utf8'), compiledShellPreload);
    } finally {
        Module.prototype.require = originalRequire;
    }
    return exposed.electronAPI;
}

function listenerHarness() {
    const listeners = new Map();
    const removed = [];
    const ipcRenderer = {
        invoke: () => Promise.resolve({ ok: true, data: {} }),
        sendSync: () => '',
        on: (channel, handler) => {
            if (!listeners.has(channel)) listeners.set(channel, new Set());
            listeners.get(channel).add(handler);
        },
        removeListener: (channel, handler) => {
            removed.push(channel);
            listeners.get(channel)?.delete(handler);
        },
    };
    return { ipcRenderer, listeners, removed };
}

test('the shell is told both phases, and only phases, over the logout channel', () => {
    const { ipcRenderer, listeners } = listenerHarness();
    const api = loadPreload(ipcRenderer);
    const seen = [];
    const off = api.onDesktopLogoutState((notice) => seen.push(notice));
    const handlers = [...(listeners.get('desktop:logout-state') || [])];
    assert.equal(handlers.length, 1, 'one subscription on the notice channel');
    handlers[0]({}, { phase: 'started', ok: false, revoked: false, code: '', message: '' });
    handlers[0]({}, { phase: 'finished', ok: true, revoked: true, code: '', message: '' });
    assert.deepEqual(seen.map((notice) => notice.phase), ['started', 'finished']);
    assert.equal(typeof off, 'function');
});

test('unsubscribing really detaches the listener', () => {
    const { ipcRenderer, listeners, removed } = listenerHarness();
    const api = loadPreload(ipcRenderer);
    const off = api.onDesktopLogoutState(() => undefined);
    off();
    assert.deepEqual(removed, ['desktop:logout-state']);
    assert.equal((listeners.get('desktop:logout-state') || new Set()).size, 0);
});

// ---------------------------------------------------------------------------
// 2.1 single-flight and the reconnect block
// ---------------------------------------------------------------------------

/** A deferred outcome so a test can hold a sign-out in flight. */
function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

test('repeated sign-outs share one detach/logout, not a race of two', async () => {
    let calls = 0;
    const gate = deferred();
    const coordinator = new signout.SignOutCoordinator(async () => {
        calls += 1;
        return gate.promise;
    });
    const first = coordinator.signOut();
    const second = coordinator.signOut();
    assert.equal(coordinator.active, true, 'a sign-out is in flight');
    assert.equal(calls, 1, 'the second call must merge into the first');
    gate.resolve({ ok: true, revoked: true, message: '' });
    assert.deepEqual(await first, { ok: true, revoked: true, message: '' });
    assert.deepEqual(await second, { ok: true, revoked: true, message: '' });
    assert.equal(calls, 1);
    assert.equal(coordinator.active, false);
    assert.equal(coordinator.attachRefusal(), '');
});

test('a workbench cannot reconnect while a sign-out is unresolved', async () => {
    const gate = deferred();
    const coordinator = new signout.SignOutCoordinator(() => gate.promise);
    const pending = coordinator.signOut();
    assert.equal(coordinator.attachRefusal(), 'logout_in_progress');
    gate.resolve({ ok: true, revoked: true, message: '' });
    await pending;
    assert.equal(coordinator.attachRefusal(), '');
});

test('an unconfirmed sign-out blocks reconnection until it is retried', async () => {
    let attempt = 0;
    const coordinator = new signout.SignOutCoordinator(async () => {
        attempt += 1;
        if (attempt === 1) return { ok: false, revoked: false, message: 'the server did not confirm' };
        return { ok: true, revoked: true, message: '' };
    });
    const failed = await coordinator.signOut();
    assert.equal(failed.ok, false);
    assert.equal(coordinator.attachRefusal(), 'logout_incomplete',
        'a session the server never revoked must not be reconnected past');
    assert.equal(coordinator.blockedReason, 'logout_incomplete');

    // The error state offers a retry, and a retry is allowed to try again.
    const retried = await coordinator.signOut();
    assert.equal(retried.ok, true);
    assert.equal(coordinator.attachRefusal(), '', 'a confirmed sign-out clears the block');
});

test('a thrown sign-out is reported as incomplete, not silently swallowed', async () => {
    const coordinator = new signout.SignOutCoordinator(async () => {
        throw new Error('the server did not confirm the sign-out');
    });
    await assert.rejects(() => coordinator.signOut(), /did not confirm/);
    assert.equal(coordinator.attachRefusal(), 'logout_incomplete');
    assert.equal(coordinator.active, false, 'a failed attempt is not still in flight');
});
