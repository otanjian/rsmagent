// The directory entry's identity checks (change
// ``fix-desktop-relogin-session-sync``, task 2.4).
//
// The regression this guards: on a desktop workbench whose native session had
// already gone, choosing a local directory opened the picker, the user picked a
// folder, and the *binding* was the first thing to notice -- answering a bare
// "not signed in". Two things were wrong with that: the dialog was a promise the
// app could not keep, and the only thing the page had to say was a message with
// no way out.
//
// So this drives the real compiled dispatcher against a stubbed broker and a
// stubbed binding transport, and pins four answers:
//
//   * no native session        -> ``auth_required``, and the picker never opens;
//   * an unfinished sign-out   -> ``auth_required``, and the picker never opens;
//   * a healthy session        -> the picker *does* open (the checks must not
//                                 have become a blanket refusal);
//   * a binding the server refused for a *dead session* -> ``auth_required``,
//     while a refusal that is about something else keeps its own code and is
//     never turned into "please sign in again".
//
// The bridge is loaded with stub `electron` and `auth-broker` modules, because
// the point is the dispatcher's decisions, not Electron's.
//
// Run: node --test tests/test_desktop_directory_identity.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const Module = require('node:module');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const dist = path.join(desktop, 'dist', 'main');
const compiledHostIpc = path.join(dist, 'remote', 'remote-host-ipc.js');
const compiledLocalFiles = path.join(dist, 'remote', 'local-files-bridge.js');

function needsBuild() {
    const srcDir = path.join(desktop, 'src', 'main', 'remote');
    const sources = ['host-bridge.ts', 'remote-host-ipc.ts', 'local-files-bridge.ts']
        .map((name) => path.join(srcDir, name));
    const newest = Math.max(...sources.map((file) => fs.statSync(file).mtimeMs));
    for (const out of [compiledHostIpc, compiledLocalFiles]) {
        if (!fs.existsSync(out)) return true;
        if (newest > fs.statSync(out).mtimeMs) return true;
    }
    return false;
}

before(() => {
    if (needsBuild()) {
        execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
    }
});

const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'cow-dir-identity-'));

/** A picked directory that really exists, so "validating" is not the failure. */
function realDirectory() {
    const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-dir-pick-')));
    fs.writeFileSync(path.join(dir, 'report.csv'), 'a,b\n1,2\n', 'utf8');
    return dir;
}

/**
 * Load the compiled bridge as the container sees it.
 *
 * ``broker`` is the whole ``auth-broker`` module as far as this dispatcher is
 * concerned -- ``status()`` is the one function task 2.4 added a call to, and
 * stubbing it is what lets a test stand in the "session is gone" state at all.
 */
function loadBridge({ broker, picker }) {
    const handlers = {};
    const openedWith = [];
    const originalRequire = Module.prototype.require;
    const fakeElectron = {
        app: { isPackaged: false, getVersion: () => '0.0.1', getPath: () => userData, on: () => undefined },
        ipcMain: {
            handle: (channel, fn) => { handlers[channel] = fn; },
            removeHandler: (channel) => { delete handlers[channel]; },
            on: () => undefined,
        },
        shell: { openExternal: () => Promise.resolve() },
        dialog: {
            showSaveDialog: async () => ({ canceled: true }),
            showOpenDialog: async (...args) => {
                openedWith.push(args[args.length - 1]);
                const picked = picker ? picker() : null;
                return picked ? { canceled: false, filePaths: [picked] } : { canceled: true, filePaths: [] };
            },
        },
        BrowserWindow: { fromWebContents: () => null },
        session: { fromPartition: () => ({}) },
    };
    Module.prototype.require = function patched(id) {
        if (id === 'electron') return fakeElectron;
        // Resolved by name so the patch survives either the compiled
        // `require('../auth-broker')` or a cached sibling.
        if (typeof id === 'string' && id.endsWith('auth-broker')) return broker;
        return originalRequire.apply(this, arguments);
    };
    try {
        delete require.cache[require.resolve(compiledHostIpc)];
        return { ipc: require(compiledHostIpc), handlers, openedWith };
    } finally {
        Module.prototype.require = originalRequire;
    }
}

const SCOPE = { serverId: 'srv_1', userId: 'usr_1', tenantId: 'tnt_1', deviceId: 'dev_1' };

/** The page's context nonce. Long enough to pass the shape check (>= 22 chars). */
const NONCE = 'nonce-0123456789abcdefghijklmnop';

/** One bridge call, wired the way the preload wires it. */
function makeBridge({ broker, picker, transport, signOut } = {}) {
    const { ipc, handlers, openedWith } = loadBridge({
        broker: broker || { status: () => ({ session: { id: 'sess' }, blockedReason: '' }) },
        picker,
    });
    ipc.registerRemoteHost({
        webContents: { id: 7, send: () => undefined },
        context: () => ({
            webContents: { webContentsId: 7, frameProcessId: 100, frameRoutingId: 3 },
            shellFrame: { webContentsId: 7, frameProcessId: 100, frameRoutingId: 3 },
            origin: 'https://console.example.com',
            generation: 4,
            entryPaths: ['/chat'],
        }),
        emit: () => undefined,
        ...(signOut ? { signOut } : {}),
        ...(transport ? { bindingTransport: transport } : {}),
    });
    ipc.setupRemoteHostIPC();
    require(compiledLocalFiles).setRemoteLocalFilesEnabled(true);
    // The caller's frame identity is the event's, never the payload's -- which is
    // exactly what lets a test stand in an iframe or a replaced document.
    const callFrom = (frames, method, params, generation) => handlers['desktop:bridge:call'](
        {
            sender: { id: 7, mainFrame: { processId: 100, routingId: 3 } },
            senderFrame: {
                processId: 100, routingId: 3, url: 'https://console.example.com/chat',
                ...frames,
            },
        },
        { method, params: params || {}, generation: generation === undefined ? 4 : generation },
    );
    const call = (method, params) => callFrom({}, method, params);
    return { ipc, call, callFrom, openedWith };
}

// ---------------------------------------------------------------------------
// Before the dialog: a session that is known to be gone
// ---------------------------------------------------------------------------

test('with no native session the picker never opens and the answer is auth_required', async () => {
    const bridge = makeBridge({ broker: { status: () => ({ session: null, blockedReason: '' }) } });
    const reply = await bridge.call('chooseWorkspace', { scope: SCOPE });
    assert.equal(reply.ok, false);
    assert.equal(reply.code, 'auth_required', 'the page needs a code it can turn into a re-login');
    assert.equal(bridge.openedWith.length, 0,
        'a dialog whose binding is certain to fail must not be opened at all');
});

test('an unfinished sign-out is refused before the dialog, not after', async () => {
    const bridge = makeBridge({
        broker: { status: () => ({ session: null, blockedReason: 'logout_incomplete' }) },
    });
    const reply = await bridge.call('chooseWorkspace', { scope: SCOPE });
    assert.equal(reply.ok, false);
    assert.equal(reply.code, 'auth_required',
        'a session the server never revoked is still "no usable session" for this entry');
    assert.match(String(reply.message), /unfinished|logout_incomplete/,
        'the log still says which of the two states it was');
    assert.equal(bridge.openedWith.length, 0);
});

test('a healthy session still opens the picker', async () => {
    const picked = realDirectory();
    const bridge = makeBridge({ picker: () => picked });
    const reply = await bridge.call('chooseWorkspace', { scope: SCOPE });
    assert.equal(bridge.openedWith.length, 1, 'the check must not become a blanket refusal');
    assert.equal(reply.ok, true);
    assert.equal(reply.data.activated, true);
    assert.equal(typeof reply.data.grant.id, 'string');
    assert.equal(JSON.stringify(reply.data).includes(picked), false,
        'a grant is never a path');
});

test('the local-files switch is still what closes the entry', async () => {
    const bridge = makeBridge({ broker: { status: () => ({ session: null, blockedReason: '' }) } });
    require(compiledLocalFiles).setRemoteLocalFilesEnabled(false);
    try {
        const reply = await bridge.call('chooseWorkspace', { scope: SCOPE });
        assert.equal(reply.code, 'feature_unavailable',
            'a closed deployment is told the feature is off, not that it is signed out');
        assert.equal(bridge.openedWith.length, 0);
    } finally {
        require(compiledLocalFiles).setRemoteLocalFilesEnabled(true);
    }
});

// ---------------------------------------------------------------------------
// After the binding: what the server says decides the message
// ---------------------------------------------------------------------------

/** A picked + activated grant, so `bindContext` has something to confirm. */
async function choose(bridge) {
    const reply = await bridge.call('chooseWorkspace', { scope: SCOPE });
    assert.equal(reply.ok, true);
    return reply.data.grant;
}

function settleReply({ status, code, message }) {
    return async () => ({
        status,
        body: JSON.stringify(code ? { code, message: message || 'refused' } : { data: {} }),
    });
}

test('a binding the server refused for a dead session becomes the re-login entry', async () => {
    const bridge = makeBridge({
        picker: () => realDirectory(),
        transport: { native: settleReply({ status: 401, code: 'session_revoked' }), web: settleReply({ status: 401, code: 'session_revoked' }) },
    });
    await choose(bridge);
    const reply = await bridge.call('bindContext', {
        scope: SCOPE, label: 'proj', agentId: 'a1', businessSessionId: 's1', contextNonce: NONCE,
    });
    assert.equal(reply.ok, false);
    assert.equal(reply.code, 'auth_required', 'the page must be able to offer the re-login');
});

test('a refusal that is not about the session keeps its own code', async () => {
    const bridge = makeBridge({
        picker: () => realDirectory(),
        transport: {
            native: settleReply({ status: 403, code: 'forbidden', message: 'no client_files permission' }),
            web: settleReply({ status: 403, code: 'forbidden', message: 'no client_files permission' }),
        },
    });
    await choose(bridge);
    const reply = await bridge.call('bindContext', {
        scope: SCOPE, label: 'proj', agentId: 'a1', businessSessionId: 's1', contextNonce: NONCE,
    });
    assert.equal(reply.ok, false);
    assert.equal(reply.code, 'forbidden',
        'a permission refusal must not be rewritten as a login problem');
    assert.match(String(reply.message), /client_files/);
});

// ---------------------------------------------------------------------------
// The account action is not exempt from the sender rules
// ---------------------------------------------------------------------------

test('a sub-frame cannot end the account, and the host is never asked to', async () => {
    let called = 0;
    const bridge = makeBridge({ signOut: async () => { called += 1; return { ok: true, revoked: true, message: '' }; } });
    // A parent frame means the call is not from the shell document, whatever it
    // claims in the payload: `signOut` rides the same check as every other method.
    const reply = await bridge.callFrom(
        { routingId: 4, parent: { processId: 100, routingId: 3 } }, 'signOut', {},
    );
    assert.equal(reply.ok, false);
    assert.equal(reply.code, 'permission_denied');
    assert.equal(called, 0, 'an untrusted frame must not be able to sign the account out');
});

test('a replaced document cannot end the account either', async () => {
    let called = 0;
    const bridge = makeBridge({ signOut: async () => { called += 1; return { ok: true, revoked: true, message: '' }; } });
    // Same frame ids, an older generation: the document that was navigated away.
    const stale = await bridge.callFrom({}, 'signOut', {}, 3);
    assert.equal(stale.ok, false, 'a generation the host did not register is refused');
    assert.equal(stale.code, 'stale_context');
    assert.equal(called, 0);
});

test('the shell document itself can end the account', async () => {
    let called = 0;
    const bridge = makeBridge({ signOut: async () => { called += 1; return { ok: true, revoked: true, message: '' }; } });
    const reply = await bridge.call('signOut', {});
    assert.equal(reply.ok, true, 'the guard must not refuse the shell it exists to serve');
    assert.equal(called, 1);
});
