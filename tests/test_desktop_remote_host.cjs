// Desktop remote host boundary: real module behaviour for the container + bridge.
//
// Change ``add-desktop-remote-web-workbench``, tasks 4.4/4.7 (acceptance A06-A11
// where a pure module can decide it). The container loads a *server* page, so
// these are the questions that decide whether the machine can be reached from
// it: who is calling, where may the shell navigate, what may leave for the
// system browser, and which surfaces must never get the bridge at all.
//
// The preload is exercised for real: its compiled file is loaded with a stub
// ``electron`` module and the surface it publishes is driven, so "the page gets
// exactly the allow-listed bridge methods and no ipcRenderer" is observed, not
// grepped for.
//
// Run: node --test tests/test_desktop_remote_host.cjs

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
const compiledWebSession = path.join(dist, 'remote', 'web-session.js');
const compiledContainer = path.join(dist, 'remote', 'container.js');
const compiledPreload = path.join(dist, 'remote-preload.js');

let hostBridge;
let contract;

function needsBuild() {
    const srcDir = path.join(desktop, 'src', 'main', 'remote');
    const sources = [path.join(srcDir, 'host-bridge.ts'), path.join(srcDir, 'web-session.ts'),
        path.join(srcDir, 'container.ts'), path.join(desktop, 'src', 'main', 'remote-preload.ts')];
    const newest = Math.max(...sources.map((file) => fs.statSync(file).mtimeMs));
    for (const out of [compiledHostBridge, compiledWebSession, compiledContainer, compiledPreload]) {
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
    contract = JSON.parse(fs.readFileSync(contractPath, 'utf8'));
});

// A sentinel that must never appear in anything the page can see.
const SENTINEL = 'SENTINEL-SECRET-1f4b9c-do-not-leak';

const ORIGIN = 'https://console.example.com';
const ENTRY_PATHS = ['/', '/chat', '/admin'];

function context(overrides = {}) {
    return {
        webContents: { webContentsId: 7, frameProcessId: 100, frameRoutingId: 3 },
        shellFrame: { webContentsId: 7, frameProcessId: 100, frameRoutingId: 3 },
        origin: ORIGIN,
        generation: 4,
        entryPaths: ENTRY_PATHS,
        ...overrides,
    };
}

function sender(overrides = {}) {
    return {
        webContentsId: 7,
        frameProcessId: 100,
        frameRoutingId: 3,
        isMainFrame: true,
        hasParentFrame: false,
        url: `${ORIGIN}/chat/42`,
        generation: 4,
        ...overrides,
    };
}

// ---------------------------------------------------------------------------
// A06: the answering document must be the current one
// ---------------------------------------------------------------------------

test('the registered shell document is accepted', () => {
    assert.equal(hostBridge.checkSender(sender(), context()).ok, true);
});

test('a late call from a replaced document is a stale_context refusal', () => {
    const stale = hostBridge.checkSender(sender({ generation: 3 }), context());
    assert.equal(stale.ok, false);
    assert.equal(stale.code, 'stale_context');
    assert.equal(contract.error_codes.stale_context, 409);
    // ...and one from a document with different frame ids, too: that is a
    // navigation that happened without us registering it.
    const replaced = hostBridge.checkSender(
        sender({ frameProcessId: 101, frameRoutingId: 9 }), context());
    assert.equal(replaced.ok, false);
    assert.equal(replaced.code, 'stale_context');
});

// ---------------------------------------------------------------------------
// A08: same origin is not enough
// ---------------------------------------------------------------------------

test('another webContents, an iframe and a popup are all refused', () => {
    const other = hostBridge.checkSender(sender({ webContentsId: 8 }), context());
    assert.equal(other.ok, false);
    assert.equal(other.code, 'permission_denied');

    const iframe = hostBridge.checkSender(
        sender({ hasParentFrame: true, frameRoutingId: 4 }), context());
    assert.equal(iframe.ok, false);
    assert.equal(iframe.code, 'permission_denied');

    const notMain = hostBridge.checkSender(sender({ isMainFrame: false }), context());
    assert.equal(notMain.ok, false);
});

test('a same-origin content page cannot borrow the shell\u2019s bridge', () => {
    for (const url of [`${ORIGIN}/preview/abc`, `${ORIGIN}/uploads/x.html`,
        `${ORIGIN}/api/file/1`, `${ORIGIN}/api/desktop/meta`]) {
        const verdict = hostBridge.checkSender(sender({ url }), context());
        assert.equal(verdict.ok, false, url);
        assert.equal(verdict.code, 'permission_denied', url);
    }
});

test('a foreign origin is refused whatever the rest of the sender looks like', () => {
    const verdict = hostBridge.checkSender(sender({ url: 'https://evil.example.com/chat' }), context());
    assert.equal(verdict.ok, false);
    assert.equal(verdict.code, 'permission_denied');
});

test('preview surfaces are never given the bridge preload', () => {
    assert.equal(hostBridge.needsBridgePreload(`${ORIGIN}/`, { origin: ORIGIN }), true);
    assert.equal(hostBridge.needsBridgePreload(`${ORIGIN}/chat/1`, { origin: ORIGIN }), true);
    for (const url of [`${ORIGIN}/preview/x`, `${ORIGIN}/uploads/x.html`,
        `${ORIGIN}/api/file/x`, 'https://elsewhere.example.com/', 'file:///etc/passwd']) {
        assert.equal(hostBridge.needsBridgePreload(url, { origin: ORIGIN }), false, url);
    }
});

// ---------------------------------------------------------------------------
// Bridge surface
// ---------------------------------------------------------------------------

test('the bridge version and methods are the ones the contract declares', () => {
    assert.equal(hostBridge.BRIDGE_VERSION, contract.bridge.version);
    assert.deepEqual([...hostBridge.PHASE1_METHODS], contract.bridge.phase1_methods);
    for (const forbidden of contract.bridge.forbidden_capabilities) {
        assert.equal(hostBridge.PHASE1_METHODS.includes(forbidden), false, forbidden);
    }
});

test('a method outside the bridge version is refused', () => {
    for (const method of ['readFile', 'exec', 'invoke', 'ipcRenderer', '', null, undefined]) {
        const verdict = hostBridge.checkBridgeCall({ method });
        assert.equal(verdict.ok, false, String(method));
        assert.equal(verdict.code, 'feature_unavailable', String(method));
    }
});

test('saveArtifact refuses a path, and only takes inline content', () => {
    const base = { name: 'report.pdf', content: 'AAAA' };
    assert.equal(hostBridge.checkBridgeCall({ method: 'saveArtifact', params: base }).ok, true);
    for (const name of ['../escape.pdf', 'a/b.pdf', 'a\\b.pdf', '..', '.', '']) {
        const verdict = hostBridge.checkBridgeCall({ method: 'saveArtifact', params: { ...base, name } });
        assert.equal(verdict.ok, false, name);
    }
    assert.equal(hostBridge.checkBridgeCall(
        { method: 'saveArtifact', params: { name: 'x.pdf', content: { path: '/etc/passwd' } } }).ok, false);
    assert.equal(hostBridge.checkBridgeCall(
        { method: 'saveArtifact', params: { name: 'x'.repeat(300), content: 'AA' } }).ok, false);
});

test('openExternal takes only a plain http(s) URL without credentials', () => {
    assert.equal(hostBridge.checkBridgeCall(
        { method: 'openExternal', params: { url: 'https://example.com/a' } }).ok, true);
    const refusals = ['file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,<b>x',
        'https://user:pass@example.com/', 'not a url', ''];
    for (const url of refusals) {
        const verdict = hostBridge.checkBridgeCall({ method: 'openExternal', params: { url } });
        assert.equal(verdict.ok, false, url);
    }
});

test('opening an external link needs a user gesture', () => {
    assert.equal(hostBridge.checkOpenExternal('https://example.com/', false).ok, false);
    assert.equal(hostBridge.checkOpenExternal('https://example.com/', true).ok, true);
});

// ---------------------------------------------------------------------------
// A09: navigation and downgrade
// ---------------------------------------------------------------------------

test('the top frame may only stay inside the shell route space', () => {
    const options = { origin: ORIGIN, entryPaths: ENTRY_PATHS };
    for (const url of [`${ORIGIN}/`, `${ORIGIN}/chat/7`, `${ORIGIN}/admin/users`]) {
        assert.equal(hostBridge.checkTopFrameNavigation(url, options).ok, true, url);
    }
    for (const url of [`${ORIGIN}/preview/x`, `${ORIGIN}/uploads/x.html`]) {
        assert.equal(hostBridge.checkTopFrameNavigation(url, options).ok, false, url);
    }
});

test('an off-origin navigation is refused and marked for the system browser', () => {
    const verdict = hostBridge.checkTopFrameNavigation('https://example.com/x',
        { origin: ORIGIN, entryPaths: ENTRY_PATHS });
    assert.equal(verdict.ok, false);
    assert.equal(verdict.external, true);
});

test('a plain-HTTP or non-web target never becomes the shell', () => {
    for (const url of ['http://console.example.com/', 'file:///etc/passwd',
        'javascript:alert(1)', 'about:blank']) {
        const verdict = hostBridge.checkTopFrameNavigation(url,
            { origin: ORIGIN, entryPaths: ENTRY_PATHS });
        assert.equal(verdict.ok, false, url);
    }
});

// ---------------------------------------------------------------------------
// W19: a content target is classified, not merely dropped
// ---------------------------------------------------------------------------
//
// The pages open files the way a browser does: a preview in a new window, an
// attachment by navigating to it. The shell may do neither, so the verdict has
// to say *which* isolated surface the target belongs to -- otherwise the
// container would refuse it silently and the pages that work in a browser would
// be dead in the container.

test('a preview or upload is classified as a document, an API path as an attachment', () => {
    const options = { origin: ORIGIN, entryPaths: ENTRY_PATHS };
    for (const url of [`${ORIGIN}/preview/abc`, `${ORIGIN}/uploads/2026/a.html`]) {
        const verdict = hostBridge.checkTopFrameNavigation(url, options);
        assert.equal(verdict.ok, false, url);
        assert.equal(verdict.content, 'document', url);
        assert.equal(verdict.external, undefined, url);
    }
    for (const url of [`${ORIGIN}/api/file?path=a.txt`,
        `${ORIGIN}/api/knowledge/sources/download?source_id=s1`,
        `${ORIGIN}/api/workspace/download?path=a.zip`]) {
        const verdict = hostBridge.checkTopFrameNavigation(url, options);
        assert.equal(verdict.ok, false, url);
        assert.equal(verdict.content, 'attachment', url);
    }
    // The classification follows the path, not a substring of the URL.
    assert.equal(hostBridge.isAttachmentPath('/api/filex'), false);
    assert.equal(hostBridge.isAttachmentPath('/api/file/1'), true);
    assert.equal(hostBridge.isContentPath('/previewer'), false);
});

// ---------------------------------------------------------------------------
// W19/W20: where an opened URL actually goes, and how big an inline save may be
// ---------------------------------------------------------------------------

test('an external link goes to the system browser, a content path to the content window', () => {
    const options = { origin: ORIGIN, entryPaths: ENTRY_PATHS };
    assert.deepEqual(hostBridge.routeOpen('https://example.com/docs', options),
        { ok: true, route: 'system-browser', url: 'https://example.com/docs' });
    const preview = hostBridge.routeOpen(`${ORIGIN}/preview/abc`, options);
    assert.equal(preview.ok, true);
    assert.equal(preview.route, 'content-window');
    assert.equal(preview.kind, 'document');
    const attachment = hostBridge.routeOpen(`${ORIGIN}/api/file?path=a.txt`, options);
    assert.equal(attachment.route, 'content-window');
    assert.equal(attachment.kind, 'attachment');
});

test('a same-origin page is not an external link: the shell is never opened twice', () => {
    const options = { origin: ORIGIN, entryPaths: ENTRY_PATHS };
    for (const url of [`${ORIGIN}/chat`, `${ORIGIN}/admin/users`, `${ORIGIN}/`]) {
        const verdict = hostBridge.routeOpen(url, options);
        assert.equal(verdict.ok, false, url);
        assert.equal(verdict.code, 'unsafe_path', url);
    }
    // A target that carries credentials is refused before the origin is judged.
    assert.equal(hostBridge.routeOpen('https://user:pw@example.com/x', options).ok, false);
    assert.equal(hostBridge.routeOpen('file:///etc/passwd', options).ok, false);
});

test('an inline save is bounded, so a page cannot push a huge buffer through IPC', () => {
    const limit = hostBridge.SAVE_ARTIFACT_MAX_BYTES;
    assert.ok(limit > 0);
    // Base64 expands 3 decoded bytes to 4 characters, so a body of 4n
    // characters decodes to 3n bytes. Choose n so the payload sits just under
    // and just over the ceiling.
    const exact = (decodedBytes) => 'A'.repeat(4 * decodedBytes / 3);
    const under = exact(3 * Math.floor(limit / 3));
    assert.equal(under.length / 4 * 3, 3 * Math.floor(limit / 3));
    const ok = hostBridge.checkBridgeCall({
        method: 'saveArtifact', params: { name: 'a.bin', content: under },
    });
    assert.equal(ok.ok, true);
    const over = exact(3 * (Math.floor(limit / 3) + 1));
    const tooBig = hostBridge.checkBridgeCall({
        method: 'saveArtifact', params: { name: 'a.bin', content: over },
    });
    assert.equal(tooBig.ok, false);
    assert.equal(tooBig.code, 'limit_exceeded');
});

// ---------------------------------------------------------------------------
// The container's wiring, driven with a stub electron
// ---------------------------------------------------------------------------

/** Load the compiled container with a stub electron and expose its handlers. */
function loadContainer(overrides = {}) {
    const originalRequire = Module.prototype.require;
    const view = {
        webContents: {
            id: 7,
            events: {},
            handlers: {},
            mainFrame: { processId: 100, routingId: 3 },
            on(event, handler) { (this.events[event] || (this.events[event] = [])).push(handler); },
            once(event, handler) { this.on(event, handler); },
            setWindowOpenHandler(handler) { this.openHandler = handler; },
            close() { this.closed = true; },
            isDestroyed: () => false,
            loadURL: () => Promise.resolve(),
            send: () => undefined,
        },
        setBounds(bounds) { this.bounds = bounds; },
    };
    const window = {
        // A real getContentBounds() reports the content area in *screen*
        // coordinates, so x/y are the window's position on the desktop. The
        // double carries a non-zero origin so that copying those x/y into a
        // child view's bounds is visible here rather than only on a real screen.
        contentBounds: { x: 44, y: 46, width: 1200, height: 800 },
        getContentBounds() { return this.contentBounds; },
        contentView: { addChildView: () => undefined, removeChildView: () => undefined },
        events: {},
        on(event, handler) { (this.events[event] || (this.events[event] = [])).push(handler); },
        removeListener: () => undefined,
    };
    const fakeElectron = {
        WebContentsView: class { constructor(options) { this.webPreferences = options.webPreferences; Object.assign(this, view); } },
    };
    Module.prototype.require = function patched(id) {
        if (id === 'electron') return fakeElectron;
        return originalRequire.apply(this, arguments);
    };
    try {
        delete require.cache[require.resolve(compiledContainer)];
        const mod = require(compiledContainer);
        const options = {
            window,
            partition: 'desktop-remote-instance',
            origin: ORIGIN,
            preloadPath: '/app/dist/main/remote-preload.js',
            entryPaths: ENTRY_PATHS,
            openExternal: () => undefined,
            session: { setPermissionRequestHandler: () => undefined, setPermissionCheckHandler: () => undefined },
            ...overrides,
        };
        return { mod, options, view: mod.createRemoteContainer(options) };
    } finally {
        Module.prototype.require = originalRequire;
    }
}

test('the container never hands the bridge to a content surface', () => {
    const { view } = loadContainer();
    const prefs = view.view.webPreferences;
    assert.equal(prefs.sandbox, true);
    assert.equal(prefs.contextIsolation, true);
    assert.equal(prefs.nodeIntegration, false);
    assert.equal(prefs.nodeIntegrationInSubFrames, false);
    assert.equal(prefs.webSecurity, true);
    assert.equal(prefs.allowRunningInsecureContent, false);
    assert.equal(prefs.partition, 'desktop-remote-instance');
    assert.ok(prefs.partition.indexOf('persist:') === -1, 'the partition is memory-only');
    assert.equal(prefs.preload, '/app/dist/main/remote-preload.js');
    assert.deepEqual(prefs.additionalArguments, []);
});

test('a bridge handshake during startup survives load completion but not document replacement', () => {
    const { view } = loadContainer();
    const fire = event => (view.webContents.events[event] || []).forEach(handler => handler());
    fire('did-navigate');
    const caller = sender({generation: view.generation});
    const liveContext = () => context({generation: view.generation, shellFrame: view.shellFrame});
    assert.equal(hostBridge.checkSender(caller, liveContext()).ok, true);
    fire('did-finish-load');
    assert.equal(hostBridge.checkSender(caller, liveContext()).ok, true);
    fire('did-navigate-in-page');
    assert.equal(hostBridge.checkSender(caller, liveContext()).ok, true);
    fire('did-navigate');
    assert.equal(hostBridge.checkSender(caller, liveContext()).code, 'stale_context');
});

test('the container covers the window instead of inheriting its screen position', () => {
    const { view, options } = loadContainer();
    // getContentBounds() reports the content area in screen coordinates: this
    // window sits at (44, 46) and reports exactly that. A child view's bounds
    // are relative to its parent, so the origin must not survive into
    // setBounds(). Letting it through shifts the page right by the window's x
    // and pushes its bottom edge off the window -- the sidebar's account footer
    // is the first thing to disappear.
    assert.deepEqual(view.view.bounds, { x: 0, y: 0, width: 1200, height: 800 });

    // A resize must land on the same rectangle. The first paint and the
    // post-resize paint disagreeing is what let this ship: the wrong origin was
    // only corrected once the user happened to resize the window.
    options.window.contentBounds = { x: 30, y: 90, width: 1400, height: 900 };
    (options.window.events.resize || []).forEach((handler) => handler());
    assert.deepEqual(view.view.bounds, { x: 0, y: 0, width: 1400, height: 900 });
});

test('a content navigation opens the isolated window instead of being dropped', () => {
    const events = [];
    const { view } = loadContainer({
        openContent: (url, kind) => events.push(['content', url, kind]),
        openExternal: (url) => events.push(['external', url]),
    });
    const fire = (url) => {
        let prevented = false;
        (view.webContents.events['will-navigate'] || []).forEach((handler) => {
            handler({ preventDefault: () => { prevented = true; } }, url);
        });
        return prevented;
    };
    assert.equal(fire(`${ORIGIN}/chat/7`), false, 'a shell route is allowed through');
    assert.deepEqual(events, []);
    assert.equal(fire(`${ORIGIN}/preview/abc`), true);
    assert.deepEqual(events, [['content', `${ORIGIN}/preview/abc`, 'document']]);
    assert.equal(fire(`${ORIGIN}/api/file?path=a.txt`), true);
    assert.deepEqual(events.at(-1), ['content', `${ORIGIN}/api/file?path=a.txt`, 'attachment']);
    assert.equal(fire('https://example.com/x'), true);
    assert.deepEqual(events.at(-1), ['external', 'https://example.com/x']);
});

test('a popup is refused, and a same-origin content popup still reaches its window', () => {
    const events = [];
    const { view } = loadContainer({
        openContent: (url, kind) => events.push(['content', url, kind]),
        openExternal: (url) => events.push(['external', url]),
    });
    const open = (url) => view.webContents.openHandler({ url });
    assert.deepEqual(open(`${ORIGIN}/preview/abc`), { action: 'deny' });
    assert.deepEqual(events, [['content', `${ORIGIN}/preview/abc`, 'document']]);
    assert.deepEqual(open('https://example.com/x'), { action: 'deny' });
    assert.deepEqual(events.at(-1), ['external', 'https://example.com/x']);
    // A same-origin shell route in a popup is not a content surface: it is
    // simply refused, so a page cannot clone the shell into an unstamped view.
    assert.deepEqual(open(`${ORIGIN}/chat`), { action: 'deny' });
    assert.equal(events.length, 2);
});

test('an origin comparison is exact: userinfo, port and scheme all matter', () => {
    assert.equal(hostBridge.sameOrigin('https://a.example.com/x', 'https://a.example.com'), true);
    assert.equal(hostBridge.sameOrigin('https://a.example.com:8443/x', 'https://a.example.com'), false);
    assert.equal(hostBridge.sameOrigin('http://a.example.com/x', 'https://a.example.com'), false);
    assert.equal(hostBridge.sameOrigin('https://a.example.com@evil.com/x', 'https://a.example.com'), false);
});

// ---------------------------------------------------------------------------
// W20: permissions and dialog throttling
// ---------------------------------------------------------------------------

test('a remote page is granted no media or device permission', () => {
    const asked = ['media', 'geolocation', 'notifications', 'usb', 'serial', 'hid',
        'clipboard-read', 'midi', 'unknown-permission'];
    for (const permission of asked) {
        assert.equal(hostBridge.permissionVerdict(permission), 'deny', permission);
    }
    assert.ok(hostBridge.DENIED_PERMISSIONS.includes('media'));
});

test('the dialog limiter stops a page from spraying dialogs', () => {
    const limiter = new hostBridge.RateLimiter(3, 1000);
    assert.equal(limiter.allow(1000), true);
    assert.equal(limiter.allow(1100), true);
    assert.equal(limiter.allow(1200), true);
    assert.equal(limiter.allow(1300), false, 'the fourth call inside the window is refused');
    assert.equal(limiter.allow(2100), true, 'the window slides');
    assert.equal(limiter.pending, 2);
});

// ---------------------------------------------------------------------------
// The preload surface, driven with a stub electron module
// ---------------------------------------------------------------------------

function loadPreload() {
    const exposed = {};
    const invoked = [];
    const listeners = [];
    const fakeElectron = {
        contextBridge: {
            exposeInMainWorld: (name, api) => { exposed[name] = api; },
        },
        ipcRenderer: {
            invoke: (channel, payload) => {
                invoked.push({ channel, payload });
                if (channel === 'desktop:bridge:hello') return Promise.resolve({ ok: true, generation: 9 });
                return Promise.resolve({ ok: true, data: {} });
            },
            on: (channel, handler) => { listeners.push({ channel, handler }); },
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
    return { exposed, invoked, listeners };
}

test('the preload publishes phase-1 plus local-files methods and nothing else', () => {
    const { exposed } = loadPreload();
    const api = exposed.desktopHost;
    assert.ok(api, 'window.desktopHost must exist');
    assert.deepEqual(Object.keys(api).sort(), [...hostBridge.ALL_BRIDGE_METHODS].sort());
    // The dangerous surfaces are absent, not merely undocumented.
    for (const forbidden of ['ipcRenderer', 'invoke', 'send', 'require', 'process',
        'fs', 'child_process', 'shell', 'token', 'getToken', 'readFile']) {
        assert.equal(api[forbidden], undefined, forbidden);
    }
    assert.equal(Object.getPrototypeOf(api) === Object.prototype, true);
});

test('a bridge call carries the method, its params and the generation only', async () => {
    const { exposed, invoked } = loadPreload();
    await exposed.desktopHost.openExternal('https://example.com/');
    const call = invoked.find((entry) => entry.channel === 'desktop:bridge:call');
    assert.ok(call, 'the call channel is used');
    assert.deepEqual(Object.keys(call.payload).sort(), ['generation', 'method', 'params']);
    assert.equal(call.payload.method, 'openExternal');
    assert.equal(call.payload.generation, 9, 'the generation comes from the handshake');
});

test('no call ever smuggles a secret token into the payload', async () => {
    const { exposed, invoked } = loadPreload();
    await exposed.desktopHost.saveArtifact('a.txt', 'AAAA');
    await exposed.desktopHost.getCapabilities();
    for (const entry of invoked) {
        const blob = JSON.stringify(entry.payload || {});
        assert.equal(blob.includes(SENTINEL), false);
        assert.equal(/token|secret|cookie|password/i.test(blob), false, blob);
    }
});

test('host events are delivered only through the preload listener', async () => {
    const { exposed, listeners } = loadPreload();
    const seen = [];
    const off = exposed.desktopHost.onHostEvent((payload) => seen.push(payload));
    assert.equal(listeners.length, 1);
    listeners[0].handler({}, { type: 'suspended' });
    assert.deepEqual(seen, [{ type: 'suspended' }]);
    assert.equal(typeof off, 'function');
    assert.equal(exposed.desktopHost.ipcRenderer, undefined);
});

// ---------------------------------------------------------------------------
// The live local context for a chat (task 9.3)
// ---------------------------------------------------------------------------

test('the local-context call carries the chat it is asking about', async () => {
    const { exposed, invoked } = loadPreload();
    await exposed.desktopHost.localContext({
        agent_id: 'agent_a', business_session_id: 'session_1',
    });
    const call = invoked.find((entry) => entry.channel === 'desktop:bridge:call');
    assert.equal(call.payload.method, 'localContext');
    assert.equal(call.payload.params.agent_id, 'agent_a');
    assert.equal(call.payload.params.business_session_id, 'session_1');
});

test('the local-context call is shape-checked like every other method', () => {
    assert.deepEqual(hostBridge.checkBridgeCall({
        method: 'localContext', params: { agent_id: 'a', business_session_id: 's' },
    }), { ok: true });
    // An empty chat is a legal question ("is anything open?"), a non-string is not.
    assert.deepEqual(hostBridge.checkBridgeCall({ method: 'localContext', params: {} }), { ok: true });
    for (const params of [
        { agent_id: 42 },
        { business_session_id: { id: 'x' } },
        { agent_id: 'a'.repeat(201) },
    ]) {
        const verdict = hostBridge.checkBridgeCall({ method: 'localContext', params });
        assert.equal(verdict.ok, false, JSON.stringify(params));
        assert.equal(verdict.code, 'invalid_request');
    }
});

test('the live local context is offered only while local files are open', () => {
    const localFiles = require(path.join(dist, 'remote', 'local-files-bridge.js'));
    const payload = () => localFiles.bridgeCapabilitiesPayload({
        bridge: '1.0', generation: 1, saveAsApproval: null,
    });
    assert.equal(payload().methods.includes('localContext'), false,
        'a closed deployment does not offer the method');
    assert.equal(payload().allMethods.includes('localContext'), true,
        'the page learns the method exists without being able to use it');
    localFiles.setRemoteLocalFilesEnabled(true);
    try {
        assert.equal(payload().methods.includes('localContext'), true);
    } finally {
        localFiles.setRemoteLocalFilesEnabled(false);
    }
});

test('SAP layout IPC accepts only bounded presentation preferences', () => {
    const check=params=>hostBridge.checkBridgeCall({method:'sapWorkbenchLayout',params}).ok;
    assert.equal(check({action:'load'}),true);
    assert.equal(check({action:'save',ratio:0.4,open:true}),true);
    for(const params of [{action:'save',ratio:NaN,open:true},{action:'save',ratio:0.9,open:true},
        {action:'save',ratio:0.4,open:'yes'},{action:'save',ratio:0.4,open:true,password:'secret'},
        {action:'load',path:'/tmp/anything'}]) assert.equal(check(params),false);
});
