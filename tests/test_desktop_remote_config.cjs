// Desktop remote configuration: real module behaviour for profiles + connection.
//
// Change ``add-desktop-remote-web-workbench``, tasks 2.1 and 2.6. These two
// modules are deliberately pure Node (no Electron import) precisely so their
// validation, migration and state machine can be exercised for real instead of
// asserted by keyword. This file compiles the main process and drives the
// compiled modules.
//
// Run: node --test tests/test_desktop_remote_config.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const url = require('node:url');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const contractPath = path.join(root, 'contracts', 'desktop', 'v1.json');
const metaSamplePath = path.join(root, 'contracts', 'desktop', 'samples', 'meta.valid.json');
const compiledProfiles = path.join(desktop, 'dist', 'main', 'remote', 'profiles.js');
const compiledConnection = path.join(desktop, 'dist', 'main', 'remote', 'connection.js');
const compiledContainerSupport = path.join(desktop, 'dist', 'main', 'remote', 'container-support.js');
const compiledBroker = path.join(desktop, 'dist', 'main', 'auth-broker.js');

let profiles;
let connection;
let containerSupport;

function needsBuild() {
    const srcDir = path.join(desktop, 'src', 'main', 'remote');
    const sources = fs.readdirSync(srcDir)
        .filter((name) => name.endsWith('.ts'))
        .map((name) => path.join(srcDir, name));
    // The trusted-sender rule below lives in the broker, not in remote/.
    sources.push(path.join(desktop, 'src', 'main', 'auth-broker.ts'));
    const newestSource = Math.max(...sources.map((p) => fs.statSync(p).mtimeMs));
    for (const out of [compiledProfiles, compiledConnection, compiledContainerSupport, compiledBroker]) {
        if (!fs.existsSync(out)) return true;
        if (newestSource > fs.statSync(out).mtimeMs) return true;
    }
    return false;
}

before(() => {
    if (needsBuild()) {
        execFileSync('npm', ['run', 'build:main'], { cwd: desktop, stdio: 'inherit' });
    }
    profiles = require(compiledProfiles);
    connection = require(compiledConnection);
    containerSupport = require(compiledContainerSupport);
});

function tmpFile(name = 'desktop-config.json') {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-profiles-'));
    return path.join(dir, name);
}

// ---------------------------------------------------------------------------
// 2.1 profiles: origin validation
// ---------------------------------------------------------------------------

test('a bare HTTPS host is accepted and normalized to an origin', () => {
    assert.deepEqual(profiles.parseServerOrigin('https://console.example.com'),
        { ok: true, origin: 'https://console.example.com' });
    assert.deepEqual(profiles.parseServerOrigin('  https://console.example.com/  '),
        { ok: true, origin: 'https://console.example.com' });
});

test('an explicit port is part of the origin, a path prefix is not allowed', () => {
    assert.deepEqual(profiles.parseServerOrigin('https://console.example.com:8443'),
        { ok: true, origin: 'https://console.example.com:8443' });
    assert.deepEqual(profiles.parseServerOrigin('https://console.example.com/app'),
        { ok: false, reason: 'has_path_prefix' });
});

test('HTTP, userinfo, query and fragment are all refused by name', () => {
    assert.deepEqual(profiles.parseServerOrigin('http://console.example.com'),
        { ok: false, reason: 'scheme_not_https' });
    assert.deepEqual(profiles.parseServerOrigin('https://alice:secret@console.example.com'),
        { ok: false, reason: 'has_userinfo' });
    assert.deepEqual(profiles.parseServerOrigin('https://console.example.com/?t=1'),
        { ok: false, reason: 'has_query_or_fragment' });
    assert.deepEqual(profiles.parseServerOrigin('https://console.example.com/#x'),
        { ok: false, reason: 'has_query_or_fragment' });
    assert.deepEqual(profiles.parseServerOrigin('not a url'), { ok: false, reason: 'invalid_url' });
    assert.deepEqual(profiles.parseServerOrigin(''), { ok: false, reason: 'empty' });
});

// ---------------------------------------------------------------------------
// 2.1 profiles: defaults, migration, persistence
// ---------------------------------------------------------------------------

test('a fresh config is local mode with nothing configured', () => {
    const config = profiles.defaultConfig();
    assert.equal(config.mode, 'local');
    assert.equal(config.version, profiles.CONFIG_VERSION);
    assert.deepEqual(config.profiles, []);
    assert.equal(config.activeProfileId, null);
});

test('an upgrade keeps local mode and never invents remote state', () => {
    // The pre-versioned shape: a single serverUrl and no version field.
    const result = profiles.normalizeConfig({ mode: 'local', serverUrl: 'https://console.example.com' });
    assert.equal(result.ok, true);
    // The URL becomes a *candidate* profile; the mode is untouched.
    assert.equal(result.config.mode, 'local');
    assert.equal(result.config.profiles.length, 1);
    assert.equal(result.config.profiles[0].origin, 'https://console.example.com');
    assert.equal(result.config.version, profiles.CONFIG_VERSION);
});

test('an invalid legacy serverUrl is dropped rather than stored', () => {
    const result = profiles.normalizeConfig({ mode: 'local', serverUrl: 'http://insecure.example.com' });
    assert.equal(result.ok, true);
    assert.deepEqual(result.config.profiles, []);
    assert.equal(result.config.activeProfileId, null);
});

test('a config from a newer build is refused, not partially read', () => {
    const result = profiles.normalizeConfig({ version: profiles.CONFIG_VERSION + 1, mode: 'remote' });
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'newer_version');
});

test('duplicate origins collapse and an orphan active id is dropped', () => {
    const result = profiles.normalizeConfig({
        version: profiles.CONFIG_VERSION,
        mode: 'remote',
        profiles: [
            { id: 'a', origin: 'https://console.example.com', displayName: 'A' },
            { id: 'b', origin: 'https://console.example.com', displayName: 'dup' },
        ],
        activeProfileId: 'missing',
    });
    assert.equal(result.ok, true);
    assert.equal(result.config.profiles.length, 1);
    assert.equal(result.config.activeProfileId, null);
});

test('save/load round-trips and writes owner-only permissions', () => {
    const file = tmpFile();
    const config = { ...profiles.defaultConfig(), mode: 'remote', activeProfileId: 'srv_1' };
    config.profiles = [{ id: 'srv_1', origin: 'https://console.example.com', displayName: 'Console' }];
    profiles.saveConfig(file, config);
    const mode = fs.statSync(file).mode & 0o777;
    assert.equal(mode, 0o600, 'config must not be world-readable');
    const loaded = profiles.loadConfig(file);
    assert.equal(loaded.config.mode, 'remote');
    assert.equal(profiles.activeProfile(loaded.config).origin, 'https://console.example.com');
});

test('a missing file loads the default and a newer file is refused with a reason', () => {
    const missing = profiles.loadConfig(tmpFile('does-not-exist.json'));
    assert.equal(missing.config.mode, 'local');
    assert.equal(missing.refused, undefined);

    const file = tmpFile('newer.json');
    fs.writeFileSync(file, JSON.stringify({ version: profiles.CONFIG_VERSION + 5 }));
    const refused = profiles.loadConfig(file);
    assert.equal(refused.refused, 'newer_version');
    assert.equal(refused.config.mode, 'local');
});

test('adding the same server twice reuses its id', () => {
    let config = profiles.defaultConfig();
    const first = profiles.addServer(config, 'https://console.example.com');
    assert.equal(first.ok, true);
    assert.equal(first.existed, false);
    const second = profiles.addServer(first.config, 'https://console.example.com');
    assert.equal(second.ok, true);
    assert.equal(second.existed, true);
    assert.equal(second.id, first.id);
    assert.equal(second.config.profiles.length, 1);
});

// ---------------------------------------------------------------------------
// 2.6 connection: probing never downgrades
// ---------------------------------------------------------------------------

const META = {
    data: {
        remote_web: { implemented: true, accepted: true, configured: true, available: true, reason: '' },
        protocols: { web_session: { major: 1, minor: 0 }, bridge: { major: 1, minor: 0 }, files: { major: 1, minor: 0 } },
        entry_path: '/',
        console_entry_paths: ['/'],
        features: {},
    },
};

test('an HTTP origin is refused before any request is issued', async () => {
    let called = 0;
    const fakeFetch = async () => { called += 1; return new Response('{}'); };
    const result = await connection.probeServer('http://console.example.com', fakeFetch);
    assert.equal(result.ok, false);
    assert.equal(result.failure.kind, 'downgrade');
    assert.equal(called, 0, 'no HTTP request may ever be attempted for a public HTTP origin');

    // Mismatched allowHttpOrigin still refuses before fetch.
    const refusedPublicHttp = await connection.probeServer(
        'http://localhost:9899', fakeFetch, { allowHttpOrigin: 'http://127.0.0.1:9899' });
    assert.equal(refusedPublicHttp.ok, false);
    assert.equal(refusedPublicHttp.failure.code, 'scheme_not_https');
    assert.equal(called, 0);
});

test('the registered local backend HTTP origin may be probed', async () => {
    let called = 0;
    const fakeFetch = async (url) => {
        called += 1;
        assert.equal(url, 'http://localhost:9899/api/desktop/meta');
        return new Response(JSON.stringify({
            status: 'success',
            data: {
                remote_web: { implemented: true, accepted: true, configured: true, available: true, reason: '' },
                protocols: {
                    web_session: { major: 1, minor: 0 },
                    bridge: { major: 1, minor: 0 },
                    files: { major: 1, minor: 0 },
                },
                entry_path: '/',
                console_entry_paths: ['/'],
                features: {},
            },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    };
    const localOk = await connection.probeServer(
        'http://localhost:9899', fakeFetch, { allowHttpOrigin: 'http://localhost:9899' });
    assert.equal(localOk.ok, true);
    assert.equal(called, 1);
    assert.equal(localOk.meta.remote_web.available, true);
});

test('a TLS error is classified as tls, not as a transient network error', () => {
    const err = Object.assign(new TypeError('fetch failed'), {
        cause: { code: 'SELF_SIGNED_CERT_IN_CHAIN' },
    });
    assert.equal(connection.classifyFetchError(err).kind, 'tls');
    assert.equal(connection.isRetryable('tls'), false);
    assert.equal(connection.isRetryable('network'), true);
});

test('a probe reads and validates the metadata envelope', async () => {
    const fakeFetch = async (url, init) => {
        assert.equal(url, 'https://console.example.com/api/desktop/meta');
        assert.equal(init.redirect, 'error');
        return new Response(JSON.stringify(META), { status: 200, headers: { 'Content-Type': 'application/json' } });
    };
    const result = await connection.probeServer('https://console.example.com', fakeFetch);
    assert.equal(result.ok, true);
    assert.equal(result.meta.entry_path, '/');
    assert.deepEqual(result.meta.protocols.bridge, { major: 1, minor: 0 });
});

test('unreadable or incomplete metadata is a protocol failure', async () => {
    const broken = async () => new Response('not json', { status: 200 });
    const result = await connection.probeServer('https://console.example.com', broken);
    assert.equal(result.ok, false);
    assert.equal(result.failure.kind, 'protocol');
});

// ---------------------------------------------------------------------------
// 2.6 connection: negotiation and retry policy
// ---------------------------------------------------------------------------

function metaWith(protocols) {
    return { ...META.data, protocols };
}

test('a required protocol with a different major is a hard incompatibility', () => {
    const result = connection.negotiate(metaWith({ web_session: { major: 2, minor: 0 }, bridge: { major: 1, minor: 0 } }));
    assert.equal(result.ok, false);
    assert.equal(result.failure.kind, 'protocol');
});

test('a required protocol that is missing refuses the connection', () => {
    const result = connection.negotiate(metaWith({ bridge: { major: 1, minor: 0 } }));
    assert.equal(result.ok, false);
    assert.match(result.failure.code, /protocol_missing_web_session/);
});

test('an optional protocol with an unknown major only disables that feature', () => {
    const result = connection.negotiate(metaWith({
        web_session: { major: 1, minor: 0 },
        bridge: { major: 1, minor: 0 },
        files: { major: 9, minor: 0 },
    }));
    assert.equal(result.ok, true);
    assert.deepEqual(result.disabled, ['files']);
});

test('the retry schedule is bounded and jittered', () => {
    assert.equal(connection.retryDelay(0, () => 0.5), 1000);
    assert.equal(connection.retryDelay(5, () => 0.5), 30000);
    assert.equal(connection.retryDelay(99, () => 0.5), 30000);
    const low = connection.retryDelay(1, () => 0);
    const high = connection.retryDelay(1, () => 1);
    assert.ok(low >= 1600 && low <= 2400, String(low));
    assert.ok(high >= 1600 && high <= 2400, String(high));
});

// ---------------------------------------------------------------------------
// 2.6 connection: the state machine
// ---------------------------------------------------------------------------

test('the machine walks probe -> authorize -> bootstrap -> ready', () => {
    const m = new connection.ConnectionMachine();
    assert.equal(m.state, 'unconfigured');
    m.dispatch({ type: 'configure' });
    assert.equal(m.state, 'probing');
    m.dispatch({ type: 'probe_ok' });
    assert.equal(m.state, 'authorizing');
    m.dispatch({ type: 'authorized' });
    assert.equal(m.state, 'bootstrapping');
    m.dispatch({ type: 'ready' });
    assert.equal(m.state, 'ready');
});

test('an identity failure stops at auth_required instead of retrying', () => {
    const m = new connection.ConnectionMachine();
    m.dispatch({ type: 'configure' });
    m.dispatch({ type: 'probe_ok' });
    m.dispatch({ type: 'authorize_failed', failure: { kind: 'identity', code: 'auth_required', message: 'x' } });
    assert.equal(m.state, 'auth_required');
});

test('a protocol failure is incompatible and a TLS failure is not retried', () => {
    const m = new connection.ConnectionMachine();
    m.dispatch({ type: 'configure' });
    m.dispatch({ type: 'probe_failed', failure: { kind: 'protocol', code: 'protocol_major_bridge', message: 'x' } });
    assert.equal(m.state, 'incompatible');

    const t = new connection.ConnectionMachine();
    t.dispatch({ type: 'configure' });
    t.dispatch({ type: 'probe_failed', failure: { kind: 'tls', code: 'tls_verification_failed', message: 'x' } });
    assert.equal(t.state, 'unconfigured');
    assert.equal(t.lastFailure.kind, 'tls');
});

test('a transient loss enters reconnecting and clearing it returns to ready', () => {
    const m = new connection.ConnectionMachine();
    m.dispatch({ type: 'configure' });
    m.dispatch({ type: 'probe_ok' });
    m.dispatch({ type: 'authorized' });
    m.dispatch({ type: 'ready' });
    m.dispatch({ type: 'lost' });
    assert.equal(m.state, 'reconnecting');
    assert.equal(m.retryAttempt, 1);
    m.dispatch({ type: 'ready' });
    assert.equal(m.state, 'ready');
    assert.equal(m.retryAttempt, 0);
});

test('blocked is terminal until an explicit reset', () => {
    const m = new connection.ConnectionMachine();
    m.dispatch({ type: 'configure' });
    m.dispatch({ type: 'blocked', failure: { kind: 'identity', code: 'logout_incomplete', message: 'x' } });
    assert.equal(m.state, 'blocked');
    m.dispatch({ type: 'configure' });
    assert.equal(m.state, 'blocked', 'blocked must not be left by a passive event');
    m.dispatch({ type: 'reset' });
    assert.equal(m.state, 'unconfigured');
});

// ---------------------------------------------------------------------------
// 1.3 cross-language contract agreement
// ---------------------------------------------------------------------------

test('the client protocol table matches the shared contract document', () => {
    const contract = JSON.parse(fs.readFileSync(contractPath, 'utf8'));
    for (const [name, client] of Object.entries(connection.CLIENT_PROTOCOLS)) {
        const declared = contract.protocols[name];
        assert.ok(declared, `contract does not declare protocol ${name}`);
        assert.equal(client.major, declared.major, `${name} major drifted`);
        assert.equal(client.minor, declared.minor, `${name} minor drifted`);
        assert.equal(client.required, declared.required, `${name} required flag drifted`);
    }
});

test('the client accepts the shared, contract-valid metadata sample', () => {
    const sample = JSON.parse(fs.readFileSync(metaSamplePath, 'utf8'));
    const meta = connection.parseMeta(sample);
    assert.ok(meta, 'the shipped sample must parse');
    const result = connection.negotiate(meta);
    assert.equal(result.ok, true);
});

test('the client retry schedule matches the contract limits', () => {
    const contract = JSON.parse(fs.readFileSync(contractPath, 'utf8'));
    const steps = contract.limits.reconnect_seconds;
    for (let attempt = 0; attempt < steps.length; attempt += 1) {
        // rand() === 0.5 removes the jitter, exposing the base step.
        assert.equal(connection.retryDelay(attempt, () => 0.5), steps[attempt] * 1000, `step ${attempt}`);
    }
});

// ---------------------------------------------------------------------------
// 1.5 the legacy Electron line never loads the remote container
// ---------------------------------------------------------------------------

test('WebContentsView support starts at Electron 30', () => {
    assert.equal(containerSupport.supportsWebContentsView('22.3.27'), false);
    assert.equal(containerSupport.supportsWebContentsView('29.4.6'), false);
    assert.equal(containerSupport.supportsWebContentsView('30.0.0'), true);
    assert.equal(containerSupport.supportsWebContentsView('v33.4.11'), true);
    assert.equal(containerSupport.supportsWebContentsView(''), false);
    assert.equal(containerSupport.supportsWebContentsView(null), false);
    assert.equal(containerSupport.MIN_ELECTRON_MAJOR_FOR_CONTAINER, 30);
});

test('the legacy line is forced back to local mode, with a reason', () => {
    const refused = containerSupport.canHonourRemoteMode('22.3.27');
    assert.equal(refused.ok, false);
    assert.equal(refused.reason, 'electron_without_web_contents_view');
    assert.equal(containerSupport.canHonourRemoteMode('33.4.11').ok, true);
});

// ---------------------------------------------------------------------------
// 2.3 the trusted-sender rule the connection shell's channels sit behind
// ---------------------------------------------------------------------------
//
// The shell is reached on a hash route (`#/remote`), so the sender check has to
// look at the *document*, not at the whole URL: comparing the whole string made
// every guarded channel answer "untrusted sender" as soon as any route was
// entered, which is exactly the state the connection shell runs in. The rule is
// driven here through the compiled broker with a stub electron, because the
// electron module only exists inside a running app.

const Module = require('node:module');
// The compiled broker compares against the document sitting next to it, so the
// fixture has to name that same file rather than an invented install path.
const RENDERER_ENTRY = path.join(desktop, 'dist', 'renderer', 'index.html');
const RENDERER_DOCUMENT = url.pathToFileURL(RENDERER_ENTRY).href;

/** Load the compiled broker with a stub electron pinned to the wanted build. */
function loadBroker({ packaged = false } = {}) {
    const originalRequire = Module.prototype.require;
    const fakeElectron = {
        app: { isPackaged: packaged, getPath: () => '/tmp', on: () => undefined },
        ipcMain: { handle: () => undefined, on: () => undefined, removeHandler: () => undefined },
        shell: { openExternal: () => Promise.resolve() },
        session: { fromPartition: () => ({ cookies: { get: () => Promise.resolve([]), remove: () => Promise.resolve() } }) },
        net: {},
    };
    Module.prototype.require = function patched(id) {
        if (id === 'electron') return fakeElectron;
        return originalRequire.apply(this, arguments);
    };
    try {
        delete require.cache[require.resolve(compiledBroker)];
        return require(compiledBroker);
    } finally {
        Module.prototype.require = originalRequire;
    }
}

test('the shell document is a registered entry with or without a hash route', () => {
    const broker = loadBroker();
    const document = RENDERER_DOCUMENT;
    assert.equal(broker.isRegisteredEntryUrl(document), true, 'the bare document must be trusted');
    assert.equal(broker.isRegisteredEntryUrl(`${document}#/`), true, 'the chat route must be trusted');
    assert.equal(broker.isRegisteredEntryUrl(`${document}#/settings`), true, 'settings must be trusted');
    assert.equal(broker.isRegisteredEntryUrl(`${document}#/remote`), true,
        'the connection shell must be trusted -- it is only reachable on a hash route');
    assert.equal(broker.isRegisteredEntryUrl(`${document}?v=2#/remote`), true);
});

test('anything that is not the shipped document is refused', () => {
    const broker = loadBroker();
    const refused = [
        '',
        'file:///etc/passwd',
        `${RENDERER_DOCUMENT}.bak`,
        url.pathToFileURL('/tmp/attacker/dist/renderer/index.html').href,
        url.pathToFileURL(path.join(path.dirname(RENDERER_ENTRY), 'index.html.orig')).href,
        url.pathToFileURL(path.join(path.dirname(path.dirname(RENDERER_ENTRY)), 'index.html')).href,
        'https://evil.example/renderer/index.html',
        'https://evil.example/#/remote',
        'javascript:alert(1)',
        'not a url',
    ];
    for (const url of refused) {
        assert.equal(broker.isRegisteredEntryUrl(url), false, `${url} must be refused`);
    }
});

test('a packaged build refuses the development server; a source run accepts it', () => {
    assert.equal(loadBroker({ packaged: true }).isRegisteredEntryUrl('http://localhost:5173/'), false);
    assert.equal(loadBroker().isRegisteredEntryUrl('http://localhost:5173/'), true);
    assert.equal(loadBroker().isRegisteredEntryUrl('http://localhost:5173/#/remote'), true);
    assert.equal(loadBroker().isRegisteredEntryUrl('http://127.0.0.1:5173/'), false,
        'only the literal dev-server origin is a trusted entry');
});

test('the trusted-window check accepts the shell frame and refuses anything else', () => {
    const broker = loadBroker();
    // A real Electron frame object: the check compares the sender frame with
    // ``sender.mainFrame`` by identity, so the fixture must share the object.
    const mainFrame = { url: `${RENDERER_DOCUMENT}#/remote` };
    const webContents = { id: 1, mainFrame };
    const window = { isDestroyed: () => false, webContents };

    const trusted = (overrides = {}) => broker.isTrustedWindowFrame({
        sender: webContents,
        senderFrame: mainFrame,
        ...overrides,
    }, window);

    assert.equal(trusted(), true, 'the shell on a hash route must be trusted');
    assert.equal(trusted({ senderFrame: { url: 'https://evil.example/#/remote' } }), false,
        'a frame that is not the main frame must be refused');
    assert.equal(trusted({ senderFrame: { ...mainFrame, url: 'https://evil.example/#/remote' } }), false,
        'another origin must be refused');
    assert.equal(trusted({ sender: { id: 2 } }), false, 'another webContents must be refused');
    assert.equal(trusted({ senderFrame: null }), false,
        'a frame that is gone must not be treated as the shell');
    assert.equal(broker.isTrustedWindowFrame({ sender: webContents, senderFrame: mainFrame },
        { isDestroyed: () => true, webContents }), false);
    assert.equal(broker.isTrustedWindowFrame({ sender: webContents, senderFrame: mainFrame }, null), false);
});

// --------------------------------------------------------------------------- //
// Which origin the shell is aimed at
//
// The regression these lock down shipped as "adding a server breaks the running
// local session": ``syncBrokerOrigin`` cleared the origin in local mode
// (reasoning: never leave a *remote* origin installed), which also discarded the
// origin the bundled backend had announced. Every credentialed request then had
// nowhere to go until the app restarted.
// --------------------------------------------------------------------------- //

const compiledConfigIpc = path.join(desktop, 'dist', 'main', 'remote', 'config-ipc.js');

/**
 * Load ``config-ipc`` and the broker it writes to, sharing one module instance.
 *
 * ``config-ipc`` derives its file from ``app.getPath('userData')``, so the stub
 * points that at a private directory: a real ``desktop-remote.json`` on the
 * machine running the tests must not leak into the assertions.
 */
function loadConfigIpc({ packaged = false, electronVersion = '33.4.11', openExternal = () => Promise.resolve() } = {}) {
    const originalRequire = Module.prototype.require;
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-config-ipc-'));
    const fakeElectron = {
        app: { isPackaged: packaged, getPath: () => userData, on: () => undefined },
        ipcMain: { handle: () => undefined, on: () => undefined, removeHandler: () => undefined },
        shell: { openExternal },
        session: { fromPartition: () => ({ cookies: { get: () => Promise.resolve([]), remove: () => Promise.resolve() } }) },
        net: {},
    };
    // ``canHonourRemoteMode`` reads ``process.versions.electron`` at the call
    // site; outside an Electron process that is undefined, so remote mode would
    // be refused for the wrong reason. Pin the runtime being tested.
    const hadElectron = Object.prototype.hasOwnProperty.call(process.versions, 'electron');
    const previousElectron = process.versions.electron;
    Object.defineProperty(process.versions, 'electron', {
        value: electronVersion, writable: true, configurable: true, enumerable: true,
    });
    Module.prototype.require = function patched(id) {
        if (id === 'electron') return fakeElectron;
        return originalRequire.apply(this, arguments);
    };
    try {
        delete require.cache[require.resolve(compiledConfigIpc)];
        delete require.cache[require.resolve(compiledBroker)];
        const broker = require(compiledBroker);
        const configIpc = require(compiledConfigIpc);
        return { broker, configIpc, userData, restore: () => {
            if (hadElectron) process.versions.electron = previousElectron;
            else delete process.versions.electron;
        } };
    } finally {
        Module.prototype.require = originalRequire;
    }
}

test('the bundled backend announces the origin every credentialed request uses', () => {
    const { broker } = loadConfigIpc();
    assert.equal(broker.getBackendOrigin(), '', 'nothing is installed before a backend exists');
    assert.equal(broker.getLocalBackendOrigin(), '');

    broker.announceLocalBackendOrigin('http://127.0.0.1:9876');
    assert.equal(broker.getBackendOrigin(), 'http://127.0.0.1:9876');
    assert.equal(broker.getLocalBackendOrigin(), 'http://127.0.0.1:9876');

    // A trailing slash is the same origin, not a new one.
    broker.announceLocalBackendOrigin('http://127.0.0.1:9876/');
    assert.equal(broker.getLocalBackendOrigin(), 'http://127.0.0.1:9876');

    broker.announceLocalBackendOrigin('');
    assert.equal(broker.getBackendOrigin(), '', 'a dead backend leaves no origin to replay against');
    assert.equal(broker.getLocalBackendOrigin(), '');
});

test('syncing the config in local mode keeps the running backend reachable', () => {
    const { broker, configIpc } = loadConfigIpc();
    broker.announceLocalBackendOrigin('http://127.0.0.1:9876');

    // The config defaults to local, which is the state "add a server" runs in.
    configIpc.syncBrokerOrigin();
    assert.equal(broker.getBackendOrigin(), 'http://127.0.0.1:9876',
        'a config sync must not blank the origin the bundled backend announced');
    assert.equal(broker.getLocalBackendOrigin(), 'http://127.0.0.1:9876');
});

test('a remote config installs the active server and a return to local restores the backend', () => {
    const { broker, configIpc, restore } = loadConfigIpc();
    try {
        broker.announceLocalBackendOrigin('http://127.0.0.1:9876');

        // Remote mode with an active server: the server owns the transport.
        const added = configIpc.addServerProfile('https://console.example', 'Console');
        assert.equal(added.ok, true, `the server must be storable: ${added.reason}`);
        assert.equal(configIpc.setActiveServer(added.id).ok, true);
        const applied = configIpc.setMode('remote');
        assert.equal(applied.applied, true, `this runtime must honour remote mode: ${applied.reason}`);
        configIpc.syncBrokerOrigin();
        assert.equal(broker.getBackendOrigin(), 'https://console.example');

        // Back to local: the bundled backend is restored, not left cleared.
        assert.equal(configIpc.setMode('local').applied, true);
        configIpc.syncBrokerOrigin();
        assert.equal(broker.getBackendOrigin(), 'http://127.0.0.1:9876');

        // Remote mode with no active server has nothing to install -- and must
        // not fall back to the local backend, which would send remote-mode
        // traffic to a different server than the config names.
        assert.equal(configIpc.setActiveServer(null).ok, true);
        configIpc.setMode('remote');
        configIpc.syncBrokerOrigin();
        assert.equal(broker.getBackendOrigin(), '', 'no active server means no origin at all');
    } finally {
        restore();
    }
});

test('a runtime that cannot host the container never takes the remote origin', () => {
    // The Windows 7 legacy line is pinned to Electron 22, which has no
    // WebContentsView: a stored ``remote`` must degrade to local, and the
    // broker must keep pointing at the bundled backend.
    const { broker, configIpc, restore } = loadConfigIpc({ electronVersion: '22.3.27' });
    try {
        broker.announceLocalBackendOrigin('http://127.0.0.1:9876');
        const added = configIpc.addServerProfile('https://console.example', 'Console');
        configIpc.setActiveServer(added.id);
        const applied = configIpc.setMode('remote');
        assert.equal(applied.applied, false, 'remote mode must not be applied on this runtime');
        configIpc.syncBrokerOrigin();
        assert.equal(broker.getBackendOrigin(), 'http://127.0.0.1:9876',
            'the bundled backend keeps the transport on an unsupported runtime');
    } finally {
        restore();
    }
});

test('consent server change persists the target and starts fresh PKCE before any token exchange', async () => {
    const originalFetch = global.fetch;
    const opened = [];
    const requests = [];
    const callback = async (authorize, params) => {
        const target = new URL(authorize.searchParams.get('redirect_uri'));
        target.search = new URLSearchParams({ state: authorize.searchParams.get('state'), ...params });
        const res = await originalFetch(target);
        await res.text();
        return res.status;
    };
    const { broker, configIpc, userData, restore } = loadConfigIpc({
        openExternal: async (raw) => {
            const authorize = new URL(raw);
            opened.push(authorize);
            if (opened.length === 1) {
                assert.equal(await callback(authorize, { state: 'wrong', error: 'server_changed',
                    server_origin: 'https://new.example' }), 400);
                assert.equal(await callback(authorize, { error: 'server_changed',
                    server_origin: 'http://new.example' }), 400);
                assert.equal(broker.getBackendOrigin(), 'http://localhost:9876');
                assert.equal(await callback(authorize, { error: 'server_changed',
                    server_origin: 'https://new.example:8443/' }), 200);
                return;
            }
            assert.equal(opened.length, 2);
            assert.equal(authorize.origin, 'https://new.example:8443');
            for (const key of ['state', 'code_challenge', 'redirect_uri']) {
                assert.notEqual(authorize.searchParams.get(key), opened[0].searchParams.get(key));
            }
            assert.equal(requests.length, 0, 'no old code or credential goes to either server');
            assert.equal(await callback(authorize, { code: 'new-server-code' }), 200);
        },
    });
    global.fetch = async (raw, options) => {
        const target = new URL(raw);
        requests.push({ target, options });
        assert.equal(target.origin, 'https://new.example:8443');
        if (target.pathname === '/auth/desktop/token') {
            const body = JSON.parse(options.body);
            assert.equal(body.code, 'new-server-code');
            assert.equal(body.redirect_uri, opened[1].searchParams.get('redirect_uri'));
            const { createHash } = require('node:crypto');
            assert.equal(createHash('sha256').update(body.code_verifier).digest('base64url'),
                opened[1].searchParams.get('code_challenge'));
            return Response.json({ token: 'new-native-token' });
        }
        assert.equal(target.pathname, '/auth/me');
        return Response.json({ status: 'success', user: { id: 'u2', username: 'new-user' }, tenants: [] });
    };
    try {
        broker.announceLocalBackendOrigin('http://localhost:9876');
        const session = await broker.beginAuthorization();
        assert.equal(session.username, 'new-user');
        assert.equal(broker.nativeSessionOrigin(), 'https://new.example:8443');
        const saved = JSON.parse(fs.readFileSync(path.join(userData, 'desktop-remote.json'), 'utf8'));
        assert.equal(saved.mode, 'remote');
        assert.equal(profiles.activeProfile(saved).origin, 'https://new.example:8443');
        assert.equal(JSON.stringify(saved).includes('new-native-token'), false);
        configIpc.selectAuthorizationServer('https://new.example:8443');
        assert.equal(configIpc.getConfig().profiles.length, 1, 'same server reuses its profile');
    } finally {
        global.fetch = originalFetch;
        restore();
        fs.rmSync(userData, { recursive: true, force: true });
    }
});

test('the login screen can select and remember a server before browser authorization', () => {
    const { broker, configIpc, userData, restore } = loadConfigIpc();
    try {
        broker.announceLocalBackendOrigin('http://localhost:9876');
        assert.equal(configIpc.modeProjection().serverOrigin, 'http://localhost:9876');
        assert.deepEqual(configIpc.selectLoginServer('http://localhost:9876/'),
            { ok: true, origin: 'http://localhost:9876' });
        assert.equal(configIpc.getConfig().mode, 'local');
        for (const value of ['', 'http://other.example', 'https://user:pass@other.example', 'https://other.example/chat']) {
            assert.equal(configIpc.selectLoginServer(value).ok, false);
            assert.equal(broker.getBackendOrigin(), 'http://localhost:9876');
        }
        assert.deepEqual(configIpc.selectLoginServer('https://NEW.example:8443/'),
            { ok: true, origin: 'https://new.example:8443' });
        assert.equal(configIpc.modeProjection().serverOrigin, 'https://new.example:8443');
        const saved = profiles.loadConfig(path.join(userData, 'desktop-remote.json')).config;
        assert.equal(saved.mode, 'remote');
        assert.equal(profiles.activeProfile(saved).origin, 'https://new.example:8443');
        assert.equal(configIpc.selectLoginServer('http://localhost:9876').ok, true);
        assert.equal(configIpc.getConfig().mode, 'local');
    } finally {
        restore();
        fs.rmSync(userData, { recursive: true, force: true });
    }
});

test('cancel during listener startup or browser wait ends cleanly and permits a fresh retry', async () => {
    let browserOpened;
    let opened = new Promise(resolve => { browserOpened = resolve; });
    const { broker, restore } = loadConfigIpc({ openExternal: async raw => browserOpened(new URL(raw)) });
    const cancelled = error => error.code === 'authorization_cancelled'
        && !error.message.includes('server or sign-in attempt changed');
    try {
        broker.announceLocalBackendOrigin('http://localhost:9876');
        const early = broker.beginAuthorization();
        broker.cancelPendingAuthorization();
        await assert.rejects(early, cancelled);
        const first = broker.beginAuthorization();
        const firstUrl = await opened;
        broker.cancelPendingAuthorization();
        await assert.rejects(first, cancelled);
        assert.equal(broker.status().session, null);
        opened = new Promise(resolve => { browserOpened = resolve; });
        const second = broker.beginAuthorization();
        const secondCancelled = assert.rejects(second, cancelled);
        const secondUrl = await opened;
        assert.notEqual(firstUrl.searchParams.get('state'), secondUrl.searchParams.get('state'));
        const target = new URL(secondUrl.searchParams.get('redirect_uri'));
        target.search = new URLSearchParams({ error: 'access_denied', state: secondUrl.searchParams.get('state') });
        const reply = await fetch(target);
        await reply.text();
        await secondCancelled;
    } finally { broker.cancelPendingAuthorization(); restore(); }
});

test('cancelling during token exchange revokes the late session only on its original server', async () => {
    const originalFetch = global.fetch;
    let releaseToken;
    let sawToken;
    const tokenRequested = new Promise(resolve => { sawToken = resolve; });
    const requests = [];
    const { broker, restore } = loadConfigIpc({ openExternal: async raw => {
        const authorize = new URL(raw);
        const callback = new URL(authorize.searchParams.get('redirect_uri'));
        callback.search = new URLSearchParams({ state: authorize.searchParams.get('state'), code: 'late-code' });
        const reply = await originalFetch(callback);
        await reply.text();
    } });
    global.fetch = async (raw, options) => {
        requests.push({ url: String(raw), options });
        if (String(raw).endsWith('/auth/desktop/token')) {
            return new Promise(resolve => { releaseToken = resolve; sawToken(); });
        }
        return Response.json({ status: 'success' });
    };
    try {
        broker.setBackendOrigin('https://old.example');
        const result = assert.rejects(broker.beginAuthorization(), error => error.code === 'authorization_cancelled');
        await tokenRequested;
        broker.cancelPendingAuthorization();
        broker.setBackendOrigin('https://new.example');
        releaseToken(Response.json({ token: 'late-session' }));
        await result;
        assert.equal(broker.status().session, null);
        assert.deepEqual(requests.map(item => item.url), [
            'https://old.example/auth/desktop/token', 'https://old.example/auth/logout',
        ]);
        assert.equal(requests[1].options.headers.Authorization, 'Bearer late-session');
    } finally { global.fetch = originalFetch; broker.cancelPendingAuthorization(); restore(); }
});

// --------------------------------------------------------------------------- //
// Signing out is a write
//
// The regression this locks down: ``fetchWithToken`` sent every credentialed
// request as a bare ``fetch`` -- i.e. GET -- including the sign-out. The server
// serves ``POST /auth/logout`` only (``channel/web/auth_handlers.py``,
// ``DbAuthLogoutHandler``), so the answer was ``405 Method Not Allowed``, the
// broker reported ``logout_incomplete`` and the paired Web child session kept
// working after the user asked to sign out. The pairing survived until it was
// revoked by hand.
//
// The broker is driven here as the app drives it: a real authorization through
// the loopback callback, against a real HTTP server that answers the same way
// the console does.
// --------------------------------------------------------------------------- //

const http = require('node:http');

/** The console's answer to the two endpoints the broker needs, plus the sign-out. */
function startFakeConsole() {
    const seen = [];
    const server = http.createServer((req, res) => {
        const chunks = [];
        req.on('data', (chunk) => chunks.push(chunk));
        req.on('end', () => {
            seen.push({ method: req.method, path: req.url.split('?')[0],
                authorization: req.headers.authorization || '',
                body: Buffer.concat(chunks).toString('utf8') });
            const answer = (status, payload) => {
                res.writeHead(status, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify(payload));
            };
            if (req.url === '/auth/desktop/token' && req.method === 'POST') {
                return answer(200, { status: 'success', token: 'native-token' });
            }
            if (req.url === '/auth/me' && req.method === 'GET') {
                return answer(200, {
                    status: 'success',
                    user: { id: 'u-1', username: 'e2e-u1', display_name: 'E2E U1',
                        is_platform_admin: false },
                    must_change_password: false,
                    tenants: [{ id: 'tnt-1', code: 'acme', name: 'Acme' }],
                });
            }
            if (req.url === '/auth/logout') {
                // Exactly the console's rule: POST signs out, anything else is
                // an unsupported method.
                if (req.method !== 'POST') {
                    res.writeHead(405, { 'Content-Type': 'text/plain' });
                    return res.end('method not allowed');
                }
                return answer(200, { status: 'success' });
            }
            answer(404, { status: 'error', message: 'not found' });
        });
    });
    return new Promise((resolve) => {
        server.listen(0, '127.0.0.1', () => resolve({
            origin: `http://127.0.0.1:${server.address().port}`,
            seen,
            close: () => new Promise((done) => server.close(done)),
        }));
    });
}

/** Load the compiled broker with a stub electron that completes the browser leg. */
function loadBrokerWithBrowserLeg() {
    const originalRequire = Module.prototype.require;
    const fakeElectron = {
        app: { isPackaged: false, getPath: () => '/tmp', on: () => undefined },
        ipcMain: { handle: () => undefined, on: () => undefined, removeHandler: () => undefined },
        shell: {
            openExternal: async (authorizeUrl) => {
                // Stand in for the human: the browser follows the authorize URL,
                // consents, and comes back on the app's loopback listener.
                const parsed = new URL(authorizeUrl);
                const redirect = new URL(parsed.searchParams.get('redirect_uri'));
                await fetch(`${redirect.origin}${redirect.pathname}` +
                    `?code=fake-code&state=${encodeURIComponent(parsed.searchParams.get('state'))}`);
            },
        },
        session: { fromPartition: () => ({ cookies: { get: () => Promise.resolve([]), remove: () => Promise.resolve() } }) },
        net: {},
    };
    Module.prototype.require = function patched(id) {
        if (id === 'electron') return fakeElectron;
        return originalRequire.apply(this, arguments);
    };
    try {
        delete require.cache[require.resolve(compiledBroker)];
        return require(compiledBroker);
    } finally {
        Module.prototype.require = originalRequire;
    }
}

test('the credentialed sign-out is sent as a write the console accepts', async () => {
    const console_ = await startFakeConsole();
    const broker = loadBrokerWithBrowserLeg();
    try {
        broker.setBackendOrigin(console_.origin);
        const projection = await broker.beginAuthorization();
        assert.equal(projection.username, 'e2e-u1', 'the authorization must produce a session');
        assert.ok(broker.status().session, 'the broker must hold the new session');

        const reply = await broker.logout();
        assert.equal(reply.ok, true, `the sign-out must succeed: ${JSON.stringify(reply)}`);
        assert.equal(reply.revoked, true, 'the server must be asked to revoke, and answer');

        const signOut = console_.seen.filter((call) => call.path === '/auth/logout');
        assert.equal(signOut.length, 1, 'exactly one sign-out call');
        assert.equal(signOut[0].method, 'POST',
            'POST /auth/logout is the only method the console serves');
        assert.equal(signOut[0].authorization, 'Bearer native-token',
            'the sign-out names the session being ended');
        assert.equal(broker.status().session, null, 'no session survives a confirmed sign-out');
    } finally {
        await console_.close();
    }
});

test('SAP layout persists in existing desktop preferences without changing server selection', () => {
    const {configIpc,userData,restore}=loadConfigIpc();
    try {
        const before=configIpc.getConfig();
        configIpc.sapWorkbenchLayout({action:'save',ratio:0.42,open:true});
        const stored=JSON.parse(fs.readFileSync(path.join(userData,'desktop-remote.json'),'utf8'));
        assert.deepEqual(stored.preferences.sapWorkbenchLayout,{ratio:0.42,open:true});
        assert.deepEqual(stored.profiles,before.profiles);
        assert.equal(stored.activeProfileId,before.activeProfileId);
        assert.deepEqual(configIpc.sapWorkbenchLayout({action:'load'}),{ratio:0.42,open:true});
    } finally {restore();}
});
