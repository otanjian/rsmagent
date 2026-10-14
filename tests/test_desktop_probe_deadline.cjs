// The metadata probe needs a deadline, and a timeout needs its own answer.
//
// Change ``harden-windows-desktop-reliability``, task 6.1, against
// openspec/changes/.../specs/desktop-tenant-context/spec.md: "Desktop SHALL 对
// 服务器 metadata、必需协议、证书、浏览器授权和 Web 配对使用有期限的探测或操作，
// 并区分网络不可达、TLS 拒绝、协议不兼容、身份失效、服务能力关闭与可选设备通道
// 故障", with the scenario "探测挂起或授权取消".
//
// The probe had no deadline at all: a server that accepted the connection and
// then said nothing left the page waiting forever. And when the wait *was*
// bounded the answer would have been "the server could not be reached", which
// sends the reader to look at their network for a server that is simply not
// answering.
//
// Run: node --test tests/test_desktop_probe_deadline.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const compiled = path.join(desktop, 'dist', 'main', 'remote', 'connection.js');
const i18nSource = fs.readFileSync(path.join(desktop, 'src', 'renderer', 'src', 'i18n.ts'), 'utf8');
const pageSource = fs.readFileSync(
    path.join(desktop, 'src', 'renderer', 'src', 'pages', 'RemoteConnectPage.tsx'), 'utf8');

let mod;

before(() => {
    const src = path.join(desktop, 'src', 'main', 'remote', 'connection.ts');
    if (!fs.existsSync(compiled) || fs.statSync(compiled).mtimeMs < fs.statSync(src).mtimeMs) {
        // The local compiler, not `npx`: on Windows `npx` may exist only as a
        // PowerShell shim, which `execFileSync` cannot launch (spawnSync ENOENT).
        execFileSync(process.execPath,
            [require.resolve('typescript/bin/tsc', { paths: [desktop] }),
                '-p', 'tsconfig.main.json'],
            { cwd: desktop, stdio: 'pipe' });
    }
    delete require.cache[require.resolve(compiled)];
    mod = require(compiled);
});

/** A fetch that answers a real, well-formed metadata envelope. */
function metaFetch(payload, status = 200) {
    return async () => ({
        status,
        json: async () => payload,
    });
}

const GOOD_META = {
    data: {
        protocols: { remote_web: { major: 1, minor: 0 } },
        entry_path: '/',
        console_entry_paths: ['/admin'],
        remote_web: { enabled: true },
    },
};

test('a server that never answers is bounded and reported as a timeout', async () => {
    const neverAnswers = (_url, init) => new Promise((_resolve, reject) => {
        // Honour the signal the probe handed us, the way a real fetch does.
        const signal = init && init.signal;
        const fail = () => reject(Object.assign(new Error('the operation was aborted'), { name: 'TimeoutError' }));
        if (signal) signal.addEventListener('abort', fail, { once: true });
    });

    const started = Date.now();
    const result = await mod.probeServer('https://example.com', neverAnswers, { timeoutMs: 50 });
    const elapsed = Date.now() - started;

    assert.equal(result.ok, false);
    assert.equal(result.failure.code, 'network_timeout');
    assert.equal(result.failure.kind, 'network');
    assert.ok(elapsed < 1500, `the probe has to return bounded, took ${elapsed}ms`);
    // Never echo the origin, and never suggest falling back to HTTP.
    assert.ok(!/example\.com/.test(JSON.stringify(result.failure)));
});

test('the shipped probe hands fetch a signal, and the default budget is 10s', () => {
    assert.equal(mod.PROBE_TIMEOUT_MS, 10000);
    let seen = 'unset';
    const capture = (_url, init) => {
        seen = init && init.signal;
        return Promise.resolve({ status: 200, json: async () => GOOD_META });
    };
    return mod.probeServer('https://example.com', capture).then((result) => {
        assert.equal(result.ok, true, 'a healthy probe still succeeds');
        assert.ok(seen && typeof seen.aborted === 'boolean',
            'probeServer must pass an AbortSignal to fetch');
    });
});

test('an abort or a socket timeout is a timeout, not "unreachable"', () => {
    for (const error of [
        Object.assign(new Error('timed out'), { name: 'TimeoutError' }),
        Object.assign(new Error('aborted'), { name: 'AbortError' }),
        Object.assign(new Error('x'), { code: 'ABORT_ERR' }),
        Object.assign(new Error('x'), { code: 'ETIMEDOUT' }),
    ]) {
        assert.equal(mod.classifyFetchError(error).code, 'network_timeout',
            `${error.name || error.code} must classify as a timeout`);
    }
});

test('the other classifications are unchanged', () => {
    const tls = mod.classifyFetchError(
        Object.assign(new Error('bad cert'), { cause: { code: 'SELF_SIGNED_CERT_IN_CHAIN' } }));
    assert.equal(tls.kind, 'tls');
    assert.equal(tls.code, 'tls_verification_failed');

    const network = mod.classifyFetchError(Object.assign(new Error('boom'), { code: 'ECONNREFUSED' }));
    assert.equal(network.code, 'network_unreachable');
});

test('a non-HTTPS origin is still refused as a downgrade, without probing', async () => {
    let called = false;
    const result = await mod.probeServer('http://example.com', async () => {
        called = true;
        return { status: 200, json: async () => GOOD_META };
    });
    assert.equal(called, false, 'an unsupported scheme must not be probed at all');
    assert.equal(result.failure.kind, 'downgrade');
});

test('unreadable metadata is still a protocol failure', async () => {
    const result = await mod.probeServer('https://example.com',
        metaFetch({ data: { protocols: {} } }));
    assert.equal(result.failure.kind, 'protocol');
});

test('the page shows the timeout as its own sentence, in both languages', () => {
    const zh = /remote_err_timeout:\s*'([^']+)'/.exec(i18nSource);
    assert.ok(zh, 'remote_err_timeout is missing from the Chinese copy');
    assert.ok(/时间|超时|回应/.test(zh[1]), `unexpected Chinese copy: ${zh[1]}`);
    const en = i18nSource.match(/remote_err_timeout:\s*'([^']+)'/g) || [];
    assert.equal(en.length, 2, 'expected the key in both language blocks');
    assert.ok(/did not answer in time/.test(en[1]), 'the English copy must say what happened');
    assert.ok(/network_timeout/.test(pageSource),
        'the page has to map the code, or the timeout would read as "unreachable"');
    assert.ok(/does not fall back to HTTP|fall back to HTTP/i.test(i18nSource),
        'the existing no-downgrade promise stays');
});
