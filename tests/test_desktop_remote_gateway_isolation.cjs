// A failed device gateway must not look like a broken server, and stopping the
// device channel must not replay anything.
//
// Change ``harden-windows-desktop-reliability``, task 6.2, against
// openspec/changes/.../specs/desktop-tenant-context/spec.md:
//
//   * "不启动本机 Python 业务后端" and the scenario "远程服务器不可用" -- a remote
//     failure must not fall back to the local backend or fabricate local history;
//   * the scenario "设备网关不可达但服务器聊天正常" -- the optional device gateway
//     failing "只显示对应设备能力故障……不阻断正常服务器业务或要求重复登录";
//   * the scenario "切换来源或运行模式" -- "旧请求不自动重放到新来源".
//
// The two channels are separate by construction (a server probe result and a
// ``DeviceClient`` state), so the test drives both at once and asserts that
// neither speaks for the other -- and that stopping the device client (what a
// mode switch does) issues no further attempt.
//
// Run: node --test tests/test_desktop_remote_gateway_isolation.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const compiledConnection = path.join(desktop, 'dist', 'main', 'remote', 'connection.js');
const compiledDeviceClient = path.join(desktop, 'dist', 'main', 'remote', 'device-client.js');
const deviceClientSource = fs.readFileSync(
    path.join(desktop, 'src', 'main', 'remote', 'device-client.ts'), 'utf8');
const mainIndexSource = fs.readFileSync(path.join(desktop, 'src', 'main', 'index.ts'), 'utf8');
const i18nSource = fs.readFileSync(
    path.join(desktop, 'src', 'renderer', 'src', 'i18n.ts'), 'utf8');

let connection;
let deviceClient;

before(() => {
    const newest = Math.max(
        fs.statSync(path.join(desktop, 'src', 'main', 'remote', 'connection.ts')).mtimeMs,
        fs.statSync(path.join(desktop, 'src', 'main', 'remote', 'device-client.ts')).mtimeMs);
    if (!fs.existsSync(compiledConnection) || fs.statSync(compiledConnection).mtimeMs < newest) {
        // The local compiler, not `npx` (see tests/test_desktop_probe_deadline.cjs).
        execFileSync(process.execPath,
            [require.resolve('typescript/bin/tsc', { paths: [desktop] }),
                '-p', 'tsconfig.main.json'],
            { cwd: desktop, stdio: 'pipe' });
    }
    delete require.cache[require.resolve(compiledConnection)];
    delete require.cache[require.resolve(compiledDeviceClient)];
    connection = require(compiledConnection);
    deviceClient = require(compiledDeviceClient);
});

const GOOD_META = {
    data: {
        protocols: { remote_web: { major: 1, minor: 0 } },
        entry_path: '/',
        console_entry_paths: ['/admin'],
        remote_web: { enabled: true },
    },
};

function goodFetch() {
    return async () => ({ status: 200, json: async () => GOOD_META });
}

function failingFetch() {
    return async () => {
        throw Object.assign(new Error('connect ECONNREFUSED'), { code: 'ECONNREFUSED' });
    };
}

/** A device client whose gateway socket can never open, recording every attempt. */
function deadGateway(options = {}) {
    const attempts = [];
    const states = [];
    const client = new deviceClient.DeviceClient({
        origin: 'https://server.example',
        token: async () => 'native-bearer',
        deviceId: async () => 'dev_1',
        runCommand: async () => ({ ok: true }),
        openSocket: (url) => {
            attempts.push(url);
            throw Object.assign(new Error('connect ECONNREFUSED 127.0.0.1'), { code: 'ECONNREFUSED' });
        },
        random: () => 1,
        onState: (state, detail) => states.push([state, detail]),
        ...options,
    });
    return { client, attempts, states };
}

const settle = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));

test('a dead device gateway does not change the server-side result', async () => {
    const probe = await connection.probeServer('https://server.example', goodFetch());
    assert.equal(probe.ok, true, 'the server side is available');

    const { client, attempts, states } = deadGateway();
    client.start();
    await settle();

    assert.equal(client.currentState, 'reconnecting',
        'the device channel reports its own state, and it is the device one');
    const detail = states.map(([, text]) => text).filter(Boolean).join(' ');
    assert.match(detail, /ECONNREFUSED/, 'the device failure keeps its own reason');
    assert.equal(attempts.length, 1);

    // The server result computed before and the device state now coexist: the
    // chat side is not "offline" just because the optional channel is.
    assert.equal(probe.ok, true);
    assert.deepEqual(probe.meta.protocols.remote_web, { major: 1, minor: 0 });

    client.stop();
    assert.equal(client.currentState, 'stopped');
});

test('stopping the device channel issues no further attempt (no replay)', async () => {
    const { client, attempts } = deadGateway();
    client.start();
    await settle();
    const before = attempts.length;
    client.stop();
    await settle(80);
    assert.equal(attempts.length, before,
        'a stopped channel must not keep retrying: a mode switch does not replay');
    assert.equal(client.currentState, 'stopped');
});

test('the device channel has no way to start the local backend', () => {
    assert.ok(!/python-manager|startBackend|PythonBackend/.test(deviceClientSource),
        'the device client must not be able to start a local Python backend');
    // And the startup branch keeps the backend to the non-remote case.
    const branch = /if \(startup\.mode === 'remote'\) \{([\s\S]*?)\n  \} else \{([\s\S]*?)\n  \}/.exec(mainIndexSource);
    assert.ok(branch, 'the startup mode branch was not found');
    assert.ok(!/startBackend\(/.test(branch[1]),
        'remote mode must not start the local backend');
    assert.match(branch[2], /startBackend\(/, 'and local mode still does');
});

test('a server failure is reported as a server failure, never as a device one', async () => {
    const probe = await connection.probeServer('https://server.example', failingFetch());
    assert.equal(probe.ok, false);
    assert.equal(probe.failure.code, 'network_unreachable');
    assert.equal(probe.failure.kind, 'network');

    // A device client that was never attached stays idle: a server failure is
    // not evidence about the optional device channel.
    const { client } = deadGateway();
    assert.equal(client.currentState, 'idle');

    // The connect page's copy for a server failure is a server sentence, and no
    // copy anywhere tells the reader the *device* is what failed there.
    assert.ok(/remote_err_network/.test(i18nSource));
    assert.ok(!/remote_err_device_offline/.test(i18nSource),
        'there is no device-offline sentence for a server failure to fall into');
});
