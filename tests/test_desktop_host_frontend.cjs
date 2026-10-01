// The Web-side environment adapter, driven in a real VM (task 5.1/5.2).
//
// The browser and the desktop container are served the *same* console, so the
// module that answers "is there a native host behind me?" has to be right in
// both, and has to be harmless when it is wrong. These cases run the shipped
// file:
//
//   * in a plain browser it exposes the full surface, resolves every call and
//     never throws -- the business pages keep one code path;
//   * with a valid bridge it forwards, and the browser fallbacks (download,
//     new tab) are never taken;
//   * with a *malformed* bridge -- including one a page forged itself -- it
//     falls back instead of issuing half-working native calls;
//   * an unknown bridge major disables the host rather than guessing;
//   * `suspendLocalContext` is what the tenant switch and the logout call,
//     and it reaches the host with the reason that was passed.
//
// Run: node --test tests/test_desktop_host_frontend.cjs

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const read = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const adapterSource = read('channel/web/static/js/fork/desktop-host.js');
const consoleJs = read('channel/web/static/js/console.js');

/**
 * A window-ish sandbox: no bridge unless the case adds one.
 *
 * The adapter is compiled once and called with a stand-in `window`. It runs in
 * *this* realm (only the global object is substituted), so the values it hands
 * back are ordinary host objects and can be compared directly.
 */
const adapterFactory = vm.runInThisContext(
    `(function (window) {\n${adapterSource}\n})`,
    { filename: 'desktop-host.js' },
);

function sandbox(extra = {}) {
    const downloads = [];
    const opened = [];
    const listeners = {};
    const window = {
        addEventListener: (name, handler) => { (listeners[name] = listeners[name] || []).push(handler); },
        setTimeout: (fn) => { void fn; return 0; },
        open: (url) => { opened.push(url); return {}; },
        atob: (value) => Buffer.from(value, 'base64').toString('binary'),
        URL: { createObjectURL: () => 'blob:test', revokeObjectURL: () => undefined },
        Blob: class { constructor(parts) { this.parts = parts; } },
        document: {
            body: {
                appendChild: (node) => downloads.push(node.download),
                removeChild: () => undefined,
            },
            createElement: () => ({ href: '', download: '', click() {} }),
        },
        ...extra,
    };
    adapterFactory(window);
    return { window, host: window.CowDesktopHost, downloads, opened, listeners };
}

/** Let queued microtasks (the adapter's promise chain) settle. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

/** A host bridge that records what the page asked of it. */
function bridge(overrides = {}) {
    const calls = [];
    const api = {
        getCapabilities: () => Promise.resolve({ bridge: '1.0', methods: ['getCapabilities', 'suspendLocalContext', 'saveArtifact', 'openExternal', 'onHostEvent'] }),
        suspendLocalContext: (reason) => { calls.push(['suspendLocalContext', reason]); return Promise.resolve({ suspended: true }); },
        saveArtifact: (name, content) => { calls.push(['saveArtifact', name, content]); return Promise.resolve({ saved: true }); },
        openExternal: (url) => { calls.push(['openExternal', url]); return Promise.resolve({ opened: true }); },
        onHostEvent: (listener) => { calls.push(['onHostEvent']); return () => undefined; },
        ...overrides,
    };
    return { api, calls };
}

// ---------------------------------------------------------------------------
// Browser default
// ---------------------------------------------------------------------------

test('a browser gets the whole surface, not a partial one', async () => {
    const { host } = sandbox();
    assert.equal(host.isDesktop(), false);
    for (const name of ['isDesktop', 'environment', 'capabilities', 'suspendLocalContext',
        'saveArtifact', 'openExternal', 'onHostEvent', 'localFiles', 'canChooseWorkspace',
        'chooseWorkspace', 'disconnectWorkspace']) {
        assert.equal(typeof host[name], 'function', name);
    }
    const info = await host.capabilities();
    assert.equal(info.available, false);
    assert.equal(info.environment, 'browser');
    assert.deepEqual(info.methods, []);
});

test('a browser no-op still resolves, so callers need no branch', async () => {
    const { host, opened } = sandbox();
    assert.deepEqual(await host.suspendLocalContext('tenant-switch'), { suspended: false, reason: 'no_host' });
    assert.equal(typeof host.onHostEvent(() => undefined), 'function');
    // ...and an unsupported scheme is refused before it reaches window.open.
    assert.deepEqual(await host.openExternal('javascript:alert(1)'),
        { opened: false, reason: 'refused' });
    assert.deepEqual(opened, []);
    assert.ok((await host.openExternal('https://example.com/x')).opened);
    assert.deepEqual(opened, ['https://example.com/x']);
});

test('a browser save falls back to a download rather than pretending', async () => {
    const { host, downloads } = sandbox();
    const result = await host.saveArtifact('report.txt', 'aGVsbG8=');
    assert.equal(result.saved, true);
    assert.equal(result.via, 'download');
    assert.deepEqual(downloads, ['report.txt']);
});

test('local file access is reported unavailable in phase one', async () => {
    const { host } = sandbox();
    assert.deepEqual(host.localFiles(), {
      available: false,
      reason: 'feature_unavailable',
      candidates: [],
      connected: null,
      transfers: [],
      note: 'directories are never auto-uploaded; choose and bind per session',
    });
    assert.equal(await host.canChooseWorkspace(), false);
    // No path-shaped API exists to be called early. chooseWorkspace /
    // disconnectWorkspace are declared but refuse while the switch is closed.
    for (const forbidden of ['readFile', 'connectWorkspace', 'getPath', 'absolutePath']) {
        assert.equal(host[forbidden], undefined, forbidden);
    }
    assert.equal(typeof host.chooseWorkspace, 'function');
    assert.equal(typeof host.disconnectWorkspace, 'function');
});

test('a host that opens localFiles updates the snapshot and forwards chooseWorkspace', async () => {
    const { api, calls } = bridge({
        getCapabilities: () => Promise.resolve({
            bridge: '1.0',
            methods: ['getCapabilities', 'suspendLocalContext', 'saveArtifact',
                'openExternal', 'onHostEvent', 'chooseWorkspace', 'disconnectWorkspace'],
            localFiles: true,
        }),
        chooseWorkspace: (scope) => {
            calls.push(['chooseWorkspace', scope]);
            return Promise.resolve({ activated: true, grant: { id: 'g1', label: 'Docs' } });
        },
    });
    const { host } = sandbox({ desktopHost: api });
    assert.equal(await host.canChooseWorkspace(), true);
    assert.equal(host.localFiles().available, true);
    const result = await host.chooseWorkspace({ serverId: 's', userId: 'u', tenantId: 't', deviceId: 'd' });
    assert.equal(result.activated, true);
    assert.deepEqual(calls[0], ['chooseWorkspace', { serverId: 's', userId: 'u', tenantId: 't', deviceId: 'd' }]);
});

// ---------------------------------------------------------------------------
// A real bridge
// ---------------------------------------------------------------------------

test('a valid bridge is used, and its own answer is what decides', async () => {
    const { api, calls } = bridge();
    const { host, opened, downloads } = sandbox({ desktopHost: api });
    assert.equal(host.isDesktop(), true);
    const info = await host.capabilities();
    assert.equal(info.available, true);
    assert.equal(info.environment, 'desktop');
    await host.suspendLocalContext('tenant-switch');
    await host.saveArtifact('a.txt', 'AAAA');
    await host.openExternal('https://example.com/');
    assert.deepEqual(calls.map((c) => c[0]), ['suspendLocalContext', 'saveArtifact', 'openExternal']);
    assert.equal(calls[0][1], 'tenant-switch');
    // The browser fallbacks were not taken.
    assert.deepEqual(opened, []);
    assert.deepEqual(downloads, []);
});

test('a bridge is validated, so a page cannot install one itself', async () => {
    const forged = { getCapabilities: () => Promise.resolve({ bridge: '1.0', methods: [] }) };
    const { host } = sandbox({ desktopHost: forged });
    assert.equal(host.isDesktop(), false, 'a partial bridge is not a host');
    const info = await host.capabilities();
    assert.equal(info.available, false);
    assert.equal(info.environment, 'browser');
});

test('an unknown bridge major disables the host instead of guessing', async () => {
    const { api } = bridge({
        getCapabilities: () => Promise.resolve({ bridge: '2.0', methods: ['getCapabilities'] }),
    });
    const { host } = sandbox({ desktopHost: api });
    const info = await host.capabilities();
    assert.equal(info.available, false);
    assert.equal(info.reason, 'bridge_incompatible');
    // A call with an incompatible host falls back, it does not throw.
    assert.deepEqual(await host.suspendLocalContext('x'), { suspended: false, reason: 'no_host' });
});

test('a host that rejects a call is not silently reported as success', async () => {
    const { api } = bridge({
        suspendLocalContext: () => Promise.reject(new Error('host gone')),
    });
    const { host } = sandbox({ desktopHost: api });
    // The rejection is the host's answer, and it must reach the caller: a page
    // that believes the suspend succeeded would keep rendering stale context.
    await assert.rejects(() => host.suspendLocalContext('logout'));
    assert.equal(host.isDesktop(), true);
});

// ---------------------------------------------------------------------------
// Page-loading lifecycle (5.2)
// ---------------------------------------------------------------------------

test('unloading the page tells the host its local context is gone', async () => {
    const { api, calls } = bridge();
    const { listeners } = sandbox({ desktopHost: api });
    assert.equal(Array.isArray(listeners.pagehide), true, 'the adapter owns the unload hook');
    listeners.pagehide.forEach((handler) => handler());
    await settle();
    assert.deepEqual(calls, [['suspendLocalContext', 'page-unload']]);
});

test('the unload hook is registered in a browser too, and stays a no-op', async () => {
    const { listeners } = sandbox();
    assert.equal(Array.isArray(listeners.pagehide), true);
    listeners.pagehide.forEach((handler) => handler());
    await settle();
});

// ---------------------------------------------------------------------------
// The console call sites
// ---------------------------------------------------------------------------

test('the tenant switch suspends the previous context before committing', () => {
    // The one-shot switch is the page's only commit site, and the call must
    // happen on the *valid* branch, after the stored tenant moves. The window
    // is generous on purpose: comments around the call are not the subject, and
    // a window that only just fits today's prose breaks on the next edit.
    const commit = consoleJs.indexOf("sessionStorage.setItem('cow_tenant_id', target);");
    assert.ok(commit > 0, 'the tenant switch commit site moved; update this test with it');
    const after = consoleJs.slice(commit, commit + 2000);
    assert.match(after, /CowDesktopHost\.suspendLocalContext\('tenant-switch'\)/);
    const suspend = after.indexOf('suspendLocalContext');
    const bump = after.indexOf('bumpTenantGeneration');
    assert.ok(bump > 0, 'the tenant generation bump moved out of the commit site');
    assert.ok(suspend < bump,
        'the host must be told before the page starts rendering the new tenant');
});

test('logging out suspends local context before the session ends', () => {
    const logout = consoleJs.indexOf('async function handleLogout()');
    assert.ok(logout > 0);
    const body = consoleJs.slice(logout, logout + 1200);
    const suspend = body.indexOf("suspendLocalContext('logout')");
    const revoke = body.indexOf("fetch('/auth/logout'");
    assert.ok(suspend > 0, 'logout must tell the host');
    assert.ok(revoke > suspend, 'the host is told before the server call');
});

test('the adapter is loaded before console.js and carries a cache-bust stamp', () => {
    const html = read('channel/web/chat.html');
    const order = [...html.matchAll(/<script defer src="assets\/(js\/[^"?]+)/g)].map((m) => m[1]);
    const adapter = order.indexOf('js/fork/desktop-host.js');
    assert.ok(adapter >= 0, 'the adapter must be on the page');
    assert.ok(adapter < order.indexOf('js/console.js'), 'console.js uses the namespace at load');
});

test('no desktop branch is scattered through the console', () => {
    // The console may *ask* the adapter; it must not reach for the raw bridge
    // or the Electron preload itself.
    assert.equal(/window\.desktopHost/.test(consoleJs), false);
    assert.equal(/window\.electronApi/.test(consoleJs), false);
    const calls = [...consoleJs.matchAll(/CowDesktopHost\.(\w+)/g)].map((m) => m[1]);
    assert.deepEqual([...new Set(calls)].sort(),
        ['bindContext', 'canChooseWorkspace', 'chooseWorkspace', 'localContext',
            'suspendLocalContext']);
});

// ---------------------------------------------------------------------------
// Window opening (task 5.6)
// ---------------------------------------------------------------------------
//
// The container denies every popup, so a page whose only way to open a preview
// or a documentation link is `window.open` would do nothing there while working
// in a browser. The adapter replaces the one seam they all share, so no page
// needs a desktop branch of its own.

test('a browser keeps its own window.open', () => {
    const original = function (...args) { return { tag: 'browser', args }; };
    const { window } = sandbox({ open: original });
    assert.equal(window.open, original, 'a browser has no host to route through');
});

test('a partial or forged bridge does not take over window.open', () => {
    const original = function () { return { tag: 'browser' }; };
    const { window } = sandbox({
        open: original,
        desktopHost: { getCapabilities: () => Promise.resolve({ bridge: '1.0', methods: [] }) },
    });
    assert.equal(window.open, original, 'the bridge is validated before it is trusted');
});

test('with a host, window.open asks the host instead of opening a popup', async () => {
    const { api, calls } = bridge();
    const { window, opened } = sandbox({ desktopHost: api });
    const handle = window.open('https://example.com/docs', '_blank', 'noopener');
    assert.equal(typeof handle, 'object', 'callers still get a handle back');
    assert.equal(handle.closed, false);
    await settle();
    assert.deepEqual(calls, [['openExternal', 'https://example.com/docs']]);
    assert.deepEqual(opened, [], 'no browser popup was attempted');
    assert.equal(window.open.__cowBridged, true);
    assert.equal(typeof window.open.__cowBrowserOpen, 'function');
});

test('with a host, a non-web target is refused by the adapter, not forwarded', async () => {
    const { api, calls } = bridge();
    const { window } = sandbox({ desktopHost: api });
    assert.equal(window.open('javascript:alert(1)'), null);
    assert.equal(window.open('about:blank'), null);
    assert.equal(window.open(''), null);
    await settle();
    assert.deepEqual(calls, []);
});

test('a host that could not open the link reports it on the handle', async () => {
    const { api } = bridge({
        openExternal: () => Promise.resolve({ opened: false, reason: 'blocked' }),
    });
    const { window } = sandbox({ desktopHost: api });
    const handle = window.open('https://example.com/x');
    await settle();
    assert.equal(handle.closed, true, 'the page can tell the window never opened');
});

test('a host that throws leaves the handle closed rather than dangling', async () => {
    const { api } = bridge({ openExternal: () => Promise.reject(new Error('gone')) });
    const { window } = sandbox({ desktopHost: api });
    const handle = window.open('https://example.com/x');
    await settle();
    assert.equal(handle.closed, true);
});
