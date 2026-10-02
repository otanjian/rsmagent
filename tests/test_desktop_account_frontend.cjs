// The desktop account adapter, driven in a real VM.
//
// Change ``fix-desktop-relogin-session-sync``, tasks 2.3 / 1.1. The regression:
// in a desktop container, signing out revoked the native session and its paired
// Web session, then reloaded the page into the *ordinary* Web password form. The
// page looked signed in, but the host had no native session, so the next local
// directory bind answered ``not signed in``.
//
// What the adapter has to guarantee, and what is driven here:
//
//   * a browser is untouched -- it has no desktop account path at all;
//   * in a container the sign-out is serial: the Web session is confirmed ended
//     *before* the host is asked to finish, and the page never reloads into a
//     plain Web login;
//   * a host that does not declare ``signOut`` is reported as outdated, so the
//     page prompts an upgrade instead of degrading to a Web-only login.
//
// The two fork modules are run as shipped, with a stand-in `window`.
//
// Run: node --test tests/test_desktop_account_frontend.cjs

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const read = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const hostSource = read('channel/web/static/js/fork/desktop-host.js');
const accountSource = read('channel/web/static/js/fork/desktop-account.js');

const hostFactory = vm.runInThisContext(
    `(function (window) {\n${hostSource}\n})`, { filename: 'desktop-host.js' },
);
const accountFactory = vm.runInThisContext(
    `(function (window) {\n${accountSource}\n})`, { filename: 'desktop-account.js' },
);

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

/** A window-ish sandbox with an observable `location.reload`. */
function sandbox(extra = {}) {
    const reloads = [];
    const window = {
        addEventListener: () => undefined,
        setTimeout: (fn) => { void fn; return 0; },
        open: () => ({}),
        atob: (value) => Buffer.from(value, 'base64').toString('binary'),
        URL: { createObjectURL: () => 'blob:test', revokeObjectURL: () => undefined },
        Blob: class { constructor(parts) { this.parts = parts; } },
        document: {
            body: { appendChild: () => undefined, removeChild: () => undefined },
            createElement: () => ({ href: '', download: '', click() {} }),
        },
        location: { reload: () => { reloads.push(Date.now()); } },
        ...extra,
    };
    hostFactory(window);
    accountFactory(window);
    return { window, host: window.CowDesktopHost, account: window.CowDesktopAccount, reloads };
}

/** A bridge that declares signOut and records the order of calls. */
function bridge(overrides = {}) {
    const calls = [];
    const api = {
        getCapabilities: () => Promise.resolve({
            bridge: '1.0',
            methods: ['getCapabilities', 'suspendLocalContext', 'saveArtifact',
                'openExternal', 'signOut', 'onHostEvent'],
        }),
        suspendLocalContext: (reason) => { calls.push(['suspendLocalContext', reason]); return Promise.resolve({ suspended: true }); },
        saveArtifact: () => Promise.resolve({ saved: true }),
        openExternal: () => Promise.resolve({ opened: true }),
        onHostEvent: () => () => undefined,
        signOut: () => {
            calls.push(['signOut']);
            return Promise.resolve({ ok: true, signedOut: true, revoked: true });
        },
        ...overrides,
    };
    return { api, calls };
}

// ---------------------------------------------------------------------------
// A browser has no desktop account path
// ---------------------------------------------------------------------------

test('a browser is not a desktop account', async () => {
    const { account } = sandbox();
    assert.equal(account.isDesktop(), false);
    assert.equal(await account.canSignOut(), false);
    const reply = await account.logout({ webLogout: () => Promise.resolve({ ok: true }) });
    assert.equal(reply.ok, false);
    assert.equal(reply.code, 'no_host');
});

// ---------------------------------------------------------------------------
// The serial sign-out
// ---------------------------------------------------------------------------

test('the Web session is confirmed ended before the host is asked to finish', async () => {
    const { api, calls } = bridge({
        signOut: () => { calls.push(['signOut']); return Promise.resolve({ ok: true, signedOut: true, revoked: true }); },
    });
    const { account, reloads } = sandbox({ desktopHost: api });
    assert.equal(account.isDesktop(), true);
    assert.equal(await account.canSignOut(), true);

    const webLogout = () => { calls.push(['webLogout']); return Promise.resolve({ ok: true }); };
    const reply = await account.logout({ webLogout });
    assert.equal(reply.ok, true);
    assert.deepEqual(calls.map((c) => c[0]), ['webLogout', 'signOut'],
        'the Web session ends first; the host finishes second');
    assert.deepEqual(reloads, [], 'the container must not reload into a plain Web login');
});

test('a Web session that did not end stops the sign-out before the host acts', async () => {
    const { api, calls } = bridge();
    const { account } = sandbox({ desktopHost: api });
    const reply = await account.logout({ webLogout: () => Promise.resolve({ ok: false }) });
    assert.equal(reply.ok, false);
    assert.equal(reply.code, 'logout_incomplete');
    assert.deepEqual(calls, [], 'the host is not asked to end a session that is still live');
});

test('a host refusal is reported as incomplete, not as success', async () => {
    const { api } = bridge({
        signOut: () => Promise.reject(Object.assign(new Error('the server did not confirm'), { code: 'logout_incomplete' })),
    });
    const { account } = sandbox({ desktopHost: api });
    const reply = await account.logout({ webLogout: () => Promise.resolve({ ok: true }) });
    assert.equal(reply.ok, false);
    assert.equal(reply.code, 'logout_incomplete');
});

// ---------------------------------------------------------------------------
// An old host is an upgrade prompt, never a silent Web-only fallback
// ---------------------------------------------------------------------------

test('a host without signOut is reported outdated and never asked for it', async () => {
    const calls = [];
    const api = {
        getCapabilities: () => Promise.resolve({
            bridge: '1.0',
            methods: ['getCapabilities', 'suspendLocalContext', 'saveArtifact', 'openExternal', 'onHostEvent'],
        }),
        suspendLocalContext: () => Promise.resolve({ suspended: true }),
        saveArtifact: () => Promise.resolve({ saved: true }),
        openExternal: () => Promise.resolve({ opened: true }),
        onHostEvent: () => () => undefined,
    };
    const { account } = sandbox({ desktopHost: api });
    assert.equal(account.isDesktop(), true, 'it is still a desktop host');
    assert.equal(await account.canSignOut(), false, 'but it cannot end the account itself');
    const reply = await account.logout({
        webLogout: () => { calls.push('webLogout'); return Promise.resolve({ ok: true }); },
    });
    assert.equal(reply.ok, false);
    assert.equal(reply.code, 'host_outdated');
    assert.deepEqual(calls, [], 'a Web-only sign-out is not a desktop recovery');
});
