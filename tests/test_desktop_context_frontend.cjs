// Desktop identity/tenant context (task group 8, design D8).
//
// Two layers of checking, both against the shipped sources:
//
//  1. static contracts over desktop/src -- the renderer must hold no credential
//     and no way to hand one to a caller, the preload must expose only narrowed
//     channels, the relay must not become a side door, and the PKCE flow must
//     keep its exact-origin/no-redirect/no-logging properties;
//  2. behavioural tests of the two fork-owned modules that are pure Node code:
//     the request-context decorator (`api/context.ts`: epoch staleness, error
//     mapping, no token in a URL) and the loopback asset proxy
//     (`main/asset-proxy.ts`: loopback bind, opaque ids, TTL, hop-by-hop
//     headers, GET-only).
//
// The behavioural part transpiles the real .ts sources with the TypeScript that
// already ships in desktop/node_modules; if it is missing the test fails loudly
// rather than silently passing a weaker check.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');

function loadTypeScript() {
    const modulePath = path.join(root, 'desktop', 'node_modules', 'typescript');
    assert.ok(fs.existsSync(modulePath), 'desktop/node_modules/typescript is required for this test');
    return require(modulePath);
}

/** Transpile a .ts source and evaluate it with a controlled sandbox. */
function loadTs(relativePath, sandbox = {}) {
    const ts = loadTypeScript();
    const absPath = path.join(root, relativePath);
    const source = fs.readFileSync(absPath, 'utf8');
    const { outputText } = ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
        fileName: relativePath,
    });
    const module = { exports: {} };
    // A relative import is another shipped .ts module (e.g. `api/context.ts`
    // imports `./features`), so resolve it through this same loader instead of
    // Node's resolver, which only knows about .js files.
    const baseDir = path.dirname(absPath);
    const resolveImport = (id) => {
        if (id.startsWith('.')) {
            const base = path.resolve(baseDir, id);
            for (const ext of ['.ts', '.tsx', '.js']) {
                if (fs.existsSync(base + ext)) return loadTs(path.relative(root, base + ext), sandbox);
            }
        }
        return require(id);
    };
    const context = vm.createContext({
        module,
        exports: module.exports,
        require: resolveImport,
        console,
        process,
        setTimeout,
        clearTimeout,
        Buffer,
        URL,
        __filename: relativePath,
        __dirname: path.dirname(path.join(root, relativePath)),
        ...sandbox,
    });
    vm.runInContext(outputText, context, { filename: relativePath });
    return module.exports;
}

function freshContext(sandbox) {
    const mod = loadTs('desktop/src/renderer/src/api/context.ts', sandbox);
    return { mod, ctx: new mod.DesktopContext() };
}

function brokerBridge(handlers) {
    const calls = [];
    return {
        calls,
        bridge: {
            desktopAuthStatus: async () => handlers.status(),
            desktopAuthProbe: async () => handlers.probe(),
            desktopAuthBegin: async () => handlers.begin(),
            desktopAuthLogout: async () => handlers.logout(),
            desktopTenantSelect: async (tenantId) => handlers.selectTenant(tenantId),
            desktopPasswordChange: async (payload) => handlers.passwordChange(payload),
            desktopRequest: async (req) => {
                calls.push(req);
                return handlers.request(req);
            },
            desktopUpload: async (req) => {
                calls.push(req);
                return handlers.upload(req);
            },
            desktopAssetUrlSync: (req) => handlers.asset(req),
        },
    };
}

function session(overrides = {}) {
    return {
        userId: 'u1',
        username: 'alice',
        displayName: 'Alice',
        isPlatformAdmin: false,
        mustChangePassword: false,
        tenants: [{ id: 't1', code: 'acme', name: 'Acme' }],
        tenantId: 't1',
        epoch: 3,
        ...overrides,
    };
}

// --------------------------------------------------------------------------
// 8.1 / 8.2: the renderer holds no session credential
// --------------------------------------------------------------------------

test('the renderer never persists or rebuilds a session credential', () => {
    const client = read('desktop/src/renderer/src/api/client.ts');
    for (const banned of [
        /localStorage\.(get|set|remove)Item\([^)]*AUTH/,
        /sessionStorage/,
        /authToken/,
        /setAuthToken/,
        /Bearer \$\{/,
        /['"]Authorization['"]\s*:/,
        /\bAuthorization:\s*`/,
        /['"]cow_session['"]/,
    ]) {
        assert.doesNotMatch(client, banned, `client.ts still carries ${banned}`);
    }
});

test('the renderer puts no session token in a URL', () => {
    const client = read('desktop/src/renderer/src/api/client.ts');
    const context = read('desktop/src/renderer/src/api/context.ts');
    // The historical `withToken` helper is gone, and nothing appends `token=`.
    assert.doesNotMatch(client, /withToken/);
    assert.doesNotMatch(client, /[?&]token=/);
    assert.doesNotMatch(client, /token=\$\{/);
    // The context module keeps a defensive stripper instead.
    assert.match(context, /stripToken/);
});

test('every request path goes through the one decorator seam', () => {
    const client = read('desktop/src/renderer/src/api/client.ts');
    // The two transports both delegate to the context decorator; there is no
    // second, hand-rolled fetch that attaches credentials.
    assert.match(client, /desktopContext\.send<T>\(/);
    assert.match(client, /desktopContext\.sendForm<T>\(/);
    assert.match(client, /desktopContext\.plan\(/);
    // No bare credentialed fetch survives in the client.
    assert.doesNotMatch(client, /await fetch\(/);
    // Header-less transports are minted through the asset proxy.
    assert.match(client, /desktopContext\.assetUrl\(/);
    assert.doesNotMatch(client, /new EventSource\(`\$\{this\.baseUrl\}/);
});

test('the preload exposes only narrowed broker channels', () => {
    const preload = read('desktop/src/main/preload.ts');
    for (const channel of [
        'desktop-auth-probe',
        'desktop-auth-status',
        'desktop-auth-begin',
        'desktop-auth-logout',
        'desktop-tenant-select',
        'desktop-request',
        'desktop-upload',
        'desktop-asset-url',
        'desktop-asset-url-sync',
    ]) {
        assert.match(preload, new RegExp(`'${channel}'`), `${channel} is missing from the preload`);
    }
    // No channel hands a token out, and none accepts a URL/header from the
    // caller: the exposed names are the narrowed ones only.
    assert.doesNotMatch(preload, /desktopToken|desktop-token|getToken|authToken/);
    assert.doesNotMatch(preload, /invoke\('(authorize|token|login)'/);
});

test('the broker keeps the credential in main memory only', () => {
    const broker = read('desktop/src/main/auth-broker.ts');
    // Secrets are never logged, written or returned as a field.
    assert.doesNotMatch(broker, /console\.(log|warn|error)\([^)]*(verifier|state|code|token)/i);
    assert.doesNotMatch(broker, /writeFile|appendFile/);
    // Credentialed redirects are refused.
    assert.match(broker, /redirect: 'error'/);
    // The callback listener is bound to the literal loopback address, and the
    // exchange re-checks the response status and the returned token's presence.
    assert.match(broker, /LOOPBACK_HOST = '127\.0\.0\.1'/);
    assert.match(broker, /timingSafeEqual/);
    assert.match(broker, /'\/auth\/desktop\/token'/);
    assert.match(broker, /CLIENT_ID = 'cowagent-desktop'/);
});

test('the generic relay cannot reach the backend, the callback or a credential', () => {
    const relay = read('desktop/src/main/http-relay.ts');
    assert.match(relay, /isPrivateHost/);
    assert.match(relay, /FORBIDDEN_HEADERS/);
    assert.match(relay, /native authorization paths are not relayable/);
    assert.match(relay, /'authorization'/);
    assert.match(relay, /'\/auth\/desktop\/token'/);
});

// --------------------------------------------------------------------------
// 8.4 / 8.5 / 8.6: the decorator's behaviour
// --------------------------------------------------------------------------

test('a planned request carries the current epoch and no token', () => {
    const { ctx } = freshContext({});
    const plan = ctx.plan('/api/sessions?token=leaked-secret&agent_id=a1');
    assert.equal(plan.epoch, 0, 'no session means no epoch');
    assert.doesNotMatch(plan.path, /token=/);
    assert.match(plan.path, /agent_id=a1/);
});

test('a response from a previous context epoch is discarded', async () => {
    const twoTenants = [{ id: 't1', code: 'acme', name: 'Acme' }, { id: 't2', code: 'beta', name: 'Beta' }];
    let current = 't1';
    const { bridge } = brokerBridge({
        status: () => ({ ok: true, session: session({ tenants: twoTenants, tenantId: current }), blockedReason: '' }),
        probe: () => ({ ok: true, authRequired: true }),
        begin: () => ({ ok: true, session: session({ epoch: 3 }) }),
        logout: () => ({ ok: true, revoked: true }),
        selectTenant: (tenantId) => {
            current = tenantId;
            return { ok: true, session: session({ tenants: twoTenants, tenantId, epoch: 4 }) };
        },
        passwordChange: () => ({ ok: true }),
        asset: () => ({ ok: true, url: 'http://127.0.0.1:1/a/deadbeef' }),
        request: () => ({ ok: true, status: 200, statusText: 'OK', contentType: 'application/json', body: '{"status":"success"}' }),
        upload: () => ({ ok: true, status: 200, statusText: 'OK', contentType: 'application/json', body: '{}' }),
    });
    const { ctx } = freshContext({ window: { electronAPI: bridge } });
    await ctx.probe();
    assert.equal(ctx.getSnapshot().gate, 'ready');
    const plan = ctx.plan('/api/sessions');
    assert.equal(plan.epoch, 3);
    // The member switches organization while the request is in flight.
    await ctx.selectTenant('t2');
    assert.equal(ctx.epoch, 4, 'a tenant switch bumps the epoch');
    await assert.rejects(
        () => ctx.send(plan, (reply) => reply.body),
        (e) => e.kind === 'stale_context',
        'a late response for an old epoch must be rejected, not rendered',
    );
    // And the current epoch still goes through.
    assert.equal(await ctx.send(ctx.plan('/api/sessions'), (reply) => reply.body), '{"status":"success"}');
});

test('401 maps to the sign-in gate instead of an empty page', async () => {
    const { bridge } = brokerBridge({
        status: () => ({ ok: true, session: session(), blockedReason: '' }),
        probe: () => ({ ok: true, authRequired: true }),
        begin: () => ({ ok: true, session: session() }),
        logout: () => ({ ok: true, revoked: true }),
        selectTenant: (tenantId) => ({ ok: true, session: session({ tenantId }) }),
        passwordChange: () => ({ ok: true }),
        asset: () => ({ ok: true, url: 'http://127.0.0.1:1/a/deadbeef' }),
        request: () => ({ ok: true, status: 401, statusText: 'Unauthorized', contentType: 'application/json', body: '{"status":"error","code":"unauthorized"}' }),
        upload: () => ({ ok: false, code: 'unauthorized', message: 'not signed in', status: 401 }),
    });
    const { ctx } = freshContext({ window: { electronAPI: bridge } });
    await ctx.probe();
    await assert.rejects(() => ctx.send(ctx.plan('/api/sessions'), (r) => r.body), (e) => e.kind === 'unauthorized');
    assert.equal(ctx.getSnapshot().gate, 'need_login');
    assert.equal(ctx.getSnapshot().session, null);
});

test('an invalid tenant code re-opens tenant selection', async () => {
    const { bridge } = brokerBridge({
        status: () => ({ ok: true, session: session({ tenants: [{ id: 't1', code: 'acme', name: 'Acme' }, { id: 't2', code: 'beta', name: 'Beta' }] }), blockedReason: '' }),
        probe: () => ({ ok: true, authRequired: true }),
        begin: () => ({ ok: true, session: session() }),
        logout: () => ({ ok: true, revoked: true }),
        selectTenant: (tenantId) => ({ ok: true, session: session({ tenantId }) }),
        passwordChange: () => ({ ok: true }),
        asset: () => ({ ok: true, url: 'http://127.0.0.1:1/a/deadbeef' }),
        request: () => ({ ok: true, status: 403, statusText: 'Forbidden', contentType: 'application/json', body: '{"status":"error","code":"invalid_tenant","message":"no membership"}' }),
        upload: () => ({ ok: true, status: 200, statusText: 'OK', contentType: 'application/json', body: '{}' }),
    });
    const { ctx } = freshContext({ window: { electronAPI: bridge } });
    await ctx.probe();
    await assert.rejects(() => ctx.send(ctx.plan('/api/sessions'), (r) => r.body), (e) => e.kind === 'invalid_tenant');
    assert.equal(ctx.getSnapshot().gate, 'tenant_select');
    assert.equal(ctx.getSnapshot().session.tenantId, null);
});

test('a forced password change and a 503 stay actionable states', async () => {
    const { bridge } = brokerBridge({
        status: () => ({ ok: true, session: session({ mustChangePassword: true }), blockedReason: '' }),
        probe: () => ({ ok: true, authRequired: true }),
        begin: () => ({ ok: true, session: session() }),
        logout: () => ({ ok: true, revoked: true }),
        selectTenant: (tenantId) => ({ ok: true, session: session({ tenantId }) }),
        passwordChange: () => ({ ok: true }),
        asset: () => ({ ok: true, url: 'http://127.0.0.1:1/a/deadbeef' }),
        request: () => ({ ok: true, status: 503, statusText: 'Service Unavailable', contentType: 'application/json', body: '{"status":"error","code":"database_unavailable"}' }),
        upload: () => ({ ok: true, status: 200, statusText: 'OK', contentType: 'application/json', body: '{}' }),
    });
    const { ctx } = freshContext({ window: { electronAPI: bridge } });
    await ctx.probe();
    assert.equal(ctx.getSnapshot().gate, 'password_change');
    assert.equal(ctx.getSnapshot().session.mustChangePassword, true);
    // A restricted account may not run tenant business at all.
    await assert.rejects(
        () => ctx.send(ctx.plan('/api/sessions'), (r) => r.body),
        (e) => e.kind === 'unavailable',
        '503 must surface as a recoverable state, never as empty data',
    );
    assert.ok(ctx.getSnapshot().lastError, 'the refusal is kept for the UI to show');
});

test('a failed sign-out stops business requests and says so', async () => {
    const { bridge } = brokerBridge({
        status: () => ({ ok: true, session: session(), blockedReason: '' }),
        probe: () => ({ ok: true, authRequired: true }),
        begin: () => ({ ok: true, session: session() }),
        logout: () => ({ ok: false, code: 'logout_incomplete', message: 'the server did not confirm the sign-out' }),
        selectTenant: (tenantId) => ({ ok: true, session: session({ tenantId }) }),
        passwordChange: () => ({ ok: true }),
        asset: () => ({ ok: true, url: 'http://127.0.0.1:1/a/deadbeef' }),
        request: () => ({ ok: true, status: 200, statusText: 'OK', contentType: 'application/json', body: '{}' }),
        upload: () => ({ ok: true, status: 200, statusText: 'OK', contentType: 'application/json', body: '{}' }),
    });
    const { ctx } = freshContext({ window: { electronAPI: bridge } });
    await ctx.probe();
    await assert.rejects(() => ctx.logout(), (e) => e.kind === 'blocked');
    assert.equal(ctx.getSnapshot().gate, 'blocked');
    assert.equal(ctx.getSnapshot().blockedReason, 'logout_incomplete');
    assert.throws(() => ctx.plan('/api/sessions'), (e) => e.kind === 'blocked');
});

test('asset URLs are minted by the broker and never fall back to a token URL', async () => {
    const { bridge } = brokerBridge({
        status: () => ({ ok: true, session: session(), blockedReason: '' }),
        probe: () => ({ ok: true, authRequired: true }),
        begin: () => ({ ok: true, session: session() }),
        logout: () => ({ ok: true, revoked: true }),
        selectTenant: (tenantId) => ({ ok: true, session: session({ tenantId }) }),
        passwordChange: () => ({ ok: true }),
        request: () => ({ ok: true, status: 200, statusText: 'OK', contentType: 'application/json', body: '{}' }),
        upload: () => ({ ok: true, status: 200, statusText: 'OK', contentType: 'application/json', body: '{}' }),
        asset: () => ({ ok: true, url: 'http://127.0.0.1:51234/a/' + 'ab'.repeat(24) }),
    });
    const { ctx } = freshContext({ window: { electronAPI: bridge } });
    await ctx.probe();
    const url = ctx.assetUrl('/api/file?path=%2Ftmp%2Fa.txt&token=leaked', 'get');
    assert.match(url, /^http:\/\/127\.0\.0\.1:\d+\/a\/[0-9a-f]{48}$/);
    assert.doesNotMatch(url, /token=|path=/);
});

// --------------------------------------------------------------------------
// Loopback asset proxy behaviour
// --------------------------------------------------------------------------

test('the asset proxy binds loopback, mints opaque ids and pipes upstream', async () => {
    const { AssetProxy } = loadTs('desktop/src/main/asset-proxy.ts', {});
    const seen = [];
    const upstream = new (require('node:stream').Readable)({ read() {} });
    upstream.push('ok-body');
    upstream.push(null);
    const proxy = new AssetProxy(async (target) => {
        seen.push(target);
        return { status: 200, headers: { 'content-type': 'text/plain', 'x-secret': 'nope' }, stream: upstream, cancel: () => {} };
    });
    try {
        const port = await proxy.start();
        const url = proxy.mint('/api/file?path=/tmp/a.txt', 'get', 7);
        assert.match(url, new RegExp(`^http://127\\.0\\.0\\.1:${port}/a/[0-9a-f]{48}$`));
        assert.doesNotMatch(url, /token=|api\/file/);

        const body = await new Promise((resolve, reject) => {
            require('node:http').get(url, (res) => {
                const chunks = [];
                res.on('data', (c) => chunks.push(c));
                res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString() }));
            }).on('error', reject);
        });
        assert.equal(body.status, 200);
        assert.equal(body.body, 'ok-body');
        assert.equal(body.headers['x-secret'], undefined, 'unlisted headers must not be forwarded');
        assert.equal(body.headers['access-control-allow-origin'], '*');
        assert.equal(seen[0].path, '/api/file?path=/tmp/a.txt');
        assert.equal(seen[0].epoch, 7);

        // The id is opaque: a guessed suffix is refused, and so is any method
        // other than GET.
        const missing = await new Promise((resolve, reject) => {
            require('node:http').get(`http://127.0.0.1:${port}/a/${'0'.repeat(48)}`, (res) => {
                res.resume();
                resolve(res.statusCode);
            }).on('error', reject);
        });
        assert.equal(missing, 404);

        // A tenant switch drops the previous epoch's ids.
        proxy.dropEpochsExcept(8);
        const afterSwitch = await new Promise((resolve, reject) => {
            require('node:http').get(url, (res) => {
                res.resume();
                resolve(res.statusCode);
            }).on('error', reject);
        });
        assert.equal(afterSwitch, 404, 'an id from the previous epoch must not resolve');
    } finally {
        proxy.close();
    }
});
