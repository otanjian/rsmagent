// Desktop half of the capability projection / scheduler contract
// (change integrate-upstream-core-capabilities, design D2 + implementation.md
// §3/§4/§6/§8).
//
// Two layers, both against the shipped sources:
//
//  1. behavioural tests of `api/features.ts` (the pure predicate) and of the
//     capability state in `api/context.ts` (clear-then-refresh on identity /
//     tenant / epoch / reconnect, stale answers dropped, gated calls refused
//     before any request);
//  2. static contracts over `api/client.ts` (legacy shapes preserved, a
//     non-success body throws, the recipient call accepts `instance_id`).
//
// The behavioural part transpiles the real .ts sources with the TypeScript that
// already ships in desktop/node_modules and loads relative .ts imports with a
// small resolver; if TypeScript is missing the test fails loudly.
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

const ts = loadTypeScript();

/** Transpile one .ts file and evaluate it with a relative-import resolver. */
function loadTsFrom(absPath, deps = {}) {
    const source = fs.readFileSync(absPath, 'utf8');
    const { outputText } = ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
        fileName: absPath,
    });
    const module = { exports: {} };
    const baseDir = path.dirname(absPath);
    const resolver = (id) => {
        if (deps[id]) return deps[id];
        if (id.startsWith('.')) {
            const candidateBase = path.resolve(baseDir, id);
            for (const ext of ['.ts', '.tsx', '.js']) {
                if (fs.existsSync(candidateBase + ext)) return loadTsFrom(candidateBase + ext, deps);
            }
            if (fs.existsSync(candidateBase)) return loadTsFrom(candidateBase, deps);
        }
        return require(id);
    };
    const context = vm.createContext({
        module,
        exports: module.exports,
        require: resolver,
        console,
        process,
        setTimeout,
        clearTimeout,
        Buffer,
        URL,
        __filename: absPath,
        __dirname: baseDir,
        ...(deps.__sandbox || {}),
    });
    vm.runInContext(outputText, context, { filename: absPath });
    return module.exports;
}

function loadTs(relativePath, sandbox = {}) {
    return loadTsFrom(path.join(root, relativePath), { __sandbox: sandbox });
}

// --------------------------------------------------------------------------
// features.ts: one pure predicate with strictly fail-closed semantics
// --------------------------------------------------------------------------

const features = loadTs('desktop/src/renderer/src/api/features.ts');

const ALL_KEYS = [
    'session_context.usage',
    'session_context.compact',
    'scheduler.instances',
    'scheduler.recipients',
    'scheduler.create',
    'scheduler.runs.list',
    'scheduler.runs.detail',
    'scheduler.runs.delete',
];

function contextWith(actions) {
    return { status: 'success', feature_actions: actions };
}

test('Desktop declares exactly the eight action keys in server order', () => {
    assert.deepEqual(JSON.parse(JSON.stringify(features.FEATURE_ACTION_KEYS)), ALL_KEYS);
});

test('Desktop mirrors the Web predicate: strict true, success status, present key', () => {
    const actions = {};
    for (const key of ALL_KEYS) actions[key] = { available: true, reason: '' };
    const open = contextWith(actions);
    for (const key of ALL_KEYS) assert.equal(features.isFeatureAvailable(open, key), true, key);

    assert.equal(features.isFeatureAvailable(contextWith({}), 'scheduler.create'), false);
    assert.equal(features.isFeatureAvailable({ status: 'error', feature_actions: actions }, 'scheduler.create'), false);
    assert.equal(features.isFeatureAvailable({ status: 'success' }, 'scheduler.create'), false);
    assert.equal(features.isFeatureAvailable(null, 'scheduler.create'), false);
    // A truthy non-boolean must never open a gate.
    for (const value of ['yes', 1, {}, [], 'true']) {
        const ctx = contextWith({ 'scheduler.create': { available: value } });
        assert.equal(features.isFeatureAvailable(ctx, 'scheduler.create'), false, JSON.stringify(value));
    }
});

test('Desktop reports the server reason for a closed action', () => {
    const ctx = contextWith({ 'session_context.compact': { available: false, reason: 'not_accepted' } });
    assert.equal(features.featureReason(ctx, 'session_context.compact'), 'not_accepted');
    assert.equal(features.featureReason(ctx, 'session_context.usage'), 'unknown');
    assert.equal(
        features.featureReason(contextWith({ 'scheduler.create': { available: true, reason: '' } }), 'scheduler.create'),
        ''
    );
});

// --------------------------------------------------------------------------
// context.ts: clear-then-refresh, revision bump, gated calls
// --------------------------------------------------------------------------

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
            desktopUpload: async (req) => handlers.upload(req),
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

function jsonReply(body) {
    return { ok: true, status: 200, statusText: 'OK', contentType: 'application/json', body: JSON.stringify(body) };
}

function twoTenantHandlers(extra = {}) {
    const twoTenants = [
        { id: 't1', code: 'acme', name: 'Acme' },
        { id: 't2', code: 'beta', name: 'Beta' },
    ];
    let current = 't1';
    return brokerBridge({
        status: () => ({ ok: true, session: session({ tenants: twoTenants, tenantId: current, epoch: current === 't1' ? 3 : 4 }), blockedReason: '' }),
        probe: () => ({ ok: true, authRequired: true }),
        begin: () => ({ ok: true, session: session() }),
        logout: () => ({ ok: true, revoked: true }),
        selectTenant: (tenantId) => {
            current = tenantId;
            return { ok: true, session: session({ tenants: twoTenants, tenantId, epoch: 4 }) };
        },
        passwordChange: () => ({ ok: true }),
        asset: () => ({ ok: true, url: 'http://127.0.0.1:1/a/deadbeef' }),
        request: (req) =>
            req.path === '/auth/context'
                ? jsonReply({ status: 'success', feature_actions: { 'scheduler.runs.list': { available: true, reason: '' } } })
                : jsonReply({ status: 'success' }),
        upload: () => jsonReply({ status: 'success' }),
        ...extra,
    });
}

test('probe loads feature_actions through the broker and gates closed keys', async () => {
    const { bridge, calls } = brokerBridge({
        status: () => ({ ok: true, session: session(), blockedReason: '' }),
        probe: () => ({ ok: true, authRequired: true }),
        begin: () => ({ ok: true, session: session() }),
        logout: () => ({ ok: true, revoked: true }),
        selectTenant: (tenantId) => ({ ok: true, session: session({ tenantId }) }),
        passwordChange: () => ({ ok: true }),
        asset: () => ({ ok: true, url: 'http://127.0.0.1:1/a/deadbeef' }),
        request: (req) =>
            req.path === '/auth/context'
                ? jsonReply({
                      status: 'success',
                      feature_actions: {
                          'session_context.usage': { available: true, reason: '' },
                          'scheduler.create': { available: false, reason: 'not_accepted' },
                      },
                  })
                : jsonReply({ status: 'success' }),
        upload: () => jsonReply({ status: 'success' }),
    });
    const mod = loadTs('desktop/src/renderer/src/api/context.ts', { window: { electronAPI: bridge } });
    const ctx = new mod.DesktopContext();
    await ctx.probe();
    await new Promise((r) => setTimeout(r, 0));

    assert.equal(ctx.isFeatureAvailable('session_context.usage'), true);
    assert.equal(ctx.isFeatureAvailable('scheduler.create'), false);
    assert.equal(ctx.featureReason('scheduler.create'), 'not_accepted');
    assert.ok(ctx.getSnapshot().featureRevision >= 1, 'the projection was (re)loaded under a revision');
    assert.ok(calls.some((c) => c.path === '/auth/context'), 'the projection is fetched via the one broker seam');

    // An open action resolves; a closed one is refused BEFORE any request.
    await ctx.requireFeature('session_context.usage');
    const before = calls.length;
    await assert.rejects(
        () => ctx.requireFeature('scheduler.create'),
        (e) => e.name === 'FeatureUnavailableError' && e.reason === 'not_accepted',
    );
    assert.equal(calls.length, before, 'a closed action must not issue a request');
});

test('a tenant switch clears the projection first and bumps the revision', async () => {
    let resolveT2 = null;
    const twoTenants = [
        { id: 't1', code: 'acme', name: 'Acme' },
        { id: 't2', code: 'beta', name: 'Beta' },
    ];
    let current = 't1';
    const { bridge } = brokerBridge({
        status: () => ({ ok: true, session: session({ tenants: twoTenants, tenantId: current, epoch: current === 't1' ? 3 : 4 }), blockedReason: '' }),
        probe: () => ({ ok: true, authRequired: true }),
        begin: () => ({ ok: true, session: session() }),
        logout: () => ({ ok: true, revoked: true }),
        selectTenant: (tenantId) => {
            current = tenantId;
            return { ok: true, session: session({ tenants: twoTenants, tenantId, epoch: 4 }) };
        },
        passwordChange: () => ({ ok: true }),
        asset: () => ({ ok: true, url: 'http://127.0.0.1:1/a/deadbeef' }),
        request: (req) => {
            if (req.path !== '/auth/context') return jsonReply({ status: 'success' });
            if (current === 't2') {
                // Hold the new tenant's answer open so the cleared state is observable.
                return new Promise((resolve) => {
                    resolveT2 = () => resolve(jsonReply({ status: 'success', feature_actions: { 'session_context.usage': { available: true, reason: '' } } }));
                });
            }
            return jsonReply({ status: 'success', feature_actions: { 'scheduler.runs.list': { available: true, reason: '' } } });
        },
        upload: () => jsonReply({ status: 'success' }),
    });
    const mod = loadTs('desktop/src/renderer/src/api/context.ts', { window: { electronAPI: bridge } });
    const ctx = new mod.DesktopContext();
    await ctx.probe();
    await new Promise((r) => setTimeout(r, 0));
    const revBefore = ctx.getSnapshot().featureRevision;
    assert.equal(ctx.isFeatureAvailable('scheduler.runs.list'), true);

    await ctx.selectTenant('t2');
    // Clear first: while the new tenant's projection is still in flight the old
    // answer is already gone, so it cannot repaint the new tenant.
    assert.equal(ctx.getSnapshot().featureActions, null);
    assert.ok(ctx.getSnapshot().featureRevision > revBefore);
    assert.equal(ctx.isFeatureAvailable('scheduler.runs.list'), false);

    resolveT2();
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(ctx.isFeatureAvailable('session_context.usage'), true);
    assert.equal(ctx.isFeatureAvailable('scheduler.runs.list'), false);
});

test('logout clears the cached projection and its revision moves', async () => {
    const { bridge } = twoTenantHandlers();
    const mod = loadTs('desktop/src/renderer/src/api/context.ts', { window: { electronAPI: bridge } });
    const ctx = new mod.DesktopContext();
    await ctx.probe();
    await new Promise((r) => setTimeout(r, 0));
    const revBefore = ctx.getSnapshot().featureRevision;
    await ctx.logout();
    assert.equal(ctx.getSnapshot().session, null);
    assert.equal(ctx.getSnapshot().featureActions, null);
    assert.ok(ctx.getSnapshot().featureRevision > revBefore);
    // Signed out: the projection is not fetched and every gated action is closed.
    assert.equal(ctx.isFeatureAvailable('scheduler.runs.list'), false);
    await assert.rejects(() => ctx.requireFeature('scheduler.runs.list'), (e) => e.name === 'FeatureUnavailableError');
});

test('a late /auth/context answer from a previous tenant is discarded', async () => {
    let resolveStale = null;
    const twoTenants = [
        { id: 't1', code: 'acme', name: 'Acme' },
        { id: 't2', code: 'beta', name: 'Beta' },
    ];
    let current = 't1';
    const { bridge } = brokerBridge({
        status: () => ({ ok: true, session: session({ tenants: twoTenants, tenantId: current, epoch: current === 't1' ? 3 : 4 }), blockedReason: '' }),
        probe: () => ({ ok: true, authRequired: true }),
        begin: () => ({ ok: true, session: session() }),
        logout: () => ({ ok: true, revoked: true }),
        selectTenant: (tenantId) => {
            current = tenantId;
            return { ok: true, session: session({ tenants: twoTenants, tenantId, epoch: 4 }) };
        },
        passwordChange: () => ({ ok: true }),
        asset: () => ({ ok: true, url: 'http://127.0.0.1:1/a/deadbeef' }),
        request: (req) => {
            if (req.path !== '/auth/context') return jsonReply({ status: 'success' });
            if (current === 't1') {
                // The old tenant's answer is held open until after the switch.
                return new Promise((resolve) => {
                    resolveStale = () => resolve(jsonReply({ status: 'success', feature_actions: { 'scheduler.create': { available: true, reason: '' } } }));
                });
            }
            return jsonReply({ status: 'success', feature_actions: { 'session_context.usage': { available: true, reason: '' } } });
        },
        upload: () => jsonReply({ status: 'success' }),
    });
    const mod = loadTs('desktop/src/renderer/src/api/context.ts', { window: { electronAPI: bridge } });
    const ctx = new mod.DesktopContext();
    await ctx.probe();
    assert.ok(resolveStale, 'the first /auth/context request is in flight');
    await ctx.selectTenant('t2');
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(ctx.isFeatureAvailable('session_context.usage'), true);

    // Now the previous tenant's answer lands. It must not reopen the old key.
    resolveStale();
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(ctx.isFeatureAvailable('scheduler.create'), false, 'a stale tenant answer must not be applied');
    assert.equal(ctx.isFeatureAvailable('session_context.usage'), true);
});

test('the capability projection is never persisted to localStorage', () => {
    const context = read('desktop/src/renderer/src/api/context.ts');
    assert.doesNotMatch(context, /localStorage\.(get|set|remove)Item/);
    assert.doesNotMatch(context, /sessionStorage/);
    assert.doesNotMatch(context, /featureActions[^\n]*JSON\.stringify/);
});

// --------------------------------------------------------------------------
// client.ts: contract shapes and fail-closed bodies
// --------------------------------------------------------------------------

test('getSchedulerRuns keeps its legacy shape and delegates to the page method', () => {
    const client = read('desktop/src/renderer/src/api/client.ts');
    assert.match(client, /async getSchedulerRunPage\([^)]*\): Promise<SchedulerRunPage>/);
    assert.match(client, /async getSchedulerRuns\([^)]*\): Promise<SchedulerRun\[\]>/);
    assert.match(client, /getSchedulerRunPage\(taskId, limit, offset\)/);
    assert.match(client, /return page\.runs/);
    assert.match(client, /history_scope: 'attributed_only'/);
});

test('a non-success body throws instead of becoming an empty list', () => {
    const client = read('desktop/src/renderer/src/api/client.ts');
    // The runs/detail/delete/instances/recipients readers all check the body.
    const checks = client.match(/if \(data\.status !== 'success'\) this\.failBody\(data\)/g) || [];
    assert.ok(checks.length >= 4, `expected the body-status guard on the new readers, found ${checks.length}`);
    assert.doesNotMatch(client, /catch\(\(\) => \[\]\)/);
});

test('the recipient call takes an optional instance_id and keeps the no-arg path', () => {
    const client = read('desktop/src/renderer/src/api/client.ts');
    assert.match(client, /async getSchedulerRecipients\(instanceId\?: string\): Promise<TaskRecipient\[\]>/);
    assert.match(client, /instance_id=\$\{encodeURIComponent\(instanceId\)\}/);
});

test('every contract action is gated before its request is issued', () => {
    const client = read('desktop/src/renderer/src/api/client.ts');
    for (const key of ALL_KEYS) {
        assert.match(client, new RegExp(`requireFeature\\('${key.replace('.', '\\.')}'\\)`), `${key} is not gated`);
    }
});
