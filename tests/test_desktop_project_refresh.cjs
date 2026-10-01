// Refreshing and losing a bound local project (change
// align-desktop-project-execution-with-master, task 9.3, A21's refresh half).
//
// The watcher notices changes *under* a project; this file is about the project
// itself: what a page that has just been (re)loaded does with a chat that is
// bound to a directory on this machine, and what happens the moment that
// authorization goes away. Both answers are only worth anything if they are
// derived live -- the page keeps no name it can fall back on, so a revoked grant
// can never be presented as an open project.
//
// Run: node --test tests/test_desktop_project_refresh.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const DESKTOP = path.join(ROOT, 'desktop');
const DIST = path.join(DESKTOP, 'dist', 'main');
const RESTORE_TS = path.join(DESKTOP, 'src', 'main', 'project-browser', 'restore.ts');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');
const consoleJs = read('channel/web/static/js/console.js');
const workspaceJs = read('channel/web/static/js/workspace.js');
const hostAdapterSource = read('channel/web/static/js/fork/desktop-host.js');
const i18nCore = read('channel/web/static/js/i18n/core.js');
const i18nJs = read('channel/web/static/js/core/i18n.js');

/**
 * The source of one function, `async` included.
 *
 * A plain `indexOf('function name(')` also matches inside `async function
 * name(`, which would silently drop the `async` and hand a test a plain value
 * where the shipped code returns a promise.
 */
function extractFunction(src, name) {
    const asyncAt = src.indexOf(`async function ${name}(`);
    let from = asyncAt >= 0 ? asyncAt : src.indexOf(`function ${name}(`);
    assert.ok(from >= 0, `Missing ${name}`);
    let depth = 0;
    for (let i = src.indexOf('{', from); i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') {
            depth--;
            if (depth === 0) return src.slice(from, i + 1);
        }
    }
    throw new Error(`Unbalanced ${name}`);
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const LIVE = {
    state: 'live',
    workspace_id: 'ws_1',
    binding_id: 'bind_1',
    grant_id: 'grant_1',
    grant_version: 3,
    selection_generation: 2,
    label: 'reports',
    executable: true,
    connected: true,
};

// ---------------------------------------------------------------------------
// The console: resuming the local project after a reload
// ---------------------------------------------------------------------------

/**
 * The console's own desktop-context functions, in a sandbox.
 *
 * The functions are the shipped ones (extracted by name, not copied), and the
 * stubs are only the page around them: a toast, the panel's `wsSourceChanged`,
 * the selector's refresh. The server answers with `serverProject`, so a test can
 * tell "the local chip survived" from "the server's project replaced it".
 */
function loadConsolePage(options = {}) {
    const toasts = [];
    const changed = [];
    const calls = [];
    const ctx = {
        console: { log() {}, warn() {}, error() {} },
        Promise, JSON, String, Number, Object, Array, parseInt, encodeURIComponent,
        setTimeout, clearTimeout,
        _wsToast: (message) => toasts.push(String(message)),
        _wsSelUpdateLabel: () => undefined,
        wsSourceChanged: () => changed.push(true),
        t: (key) => key,
        CowDesktopHost: options.host,
        document: { getElementById: () => null },
        fetch: async () => ({
            ok: true,
            status: 200,
            json: async () => options.server || {
                status: 'success',
                current: { path: '/srv/proj', name: 'proj' },
                recents: [],
                default_workspace: '/srv/agents/a',
                projects_root: '/srv/projects',
            },
        }),
    };
    ctx.activeAgentId = options.agentId === undefined ? 'agent_a' : options.agentId;
    ctx.sessionId = options.sessionId === undefined ? 'session_1' : options.sessionId;
    vm.createContext(ctx);
    vm.runInContext([
        'let _desktopContext = null;',
        'let _desktopContextKey = "";',
        'let _wsSelState = { current: null, recents: [], defaultWorkspace: "", projectsRoot: "" };',
        extractFunction(consoleJs, '_desktopSelectionKey'),
        extractFunction(consoleJs, '_desktopContextClear'),
        extractFunction(consoleJs, '_desktopContextForRequest'),
        extractFunction(consoleJs, '_desktopRestoreContext'),
        extractFunction(consoleJs, '_desktopLocalLost'),
        extractFunction(consoleJs, 'refreshWorkspaceSelector'),
    ].join('\n'), ctx);
    const evaluate = (expr) => vm.runInContext(expr, ctx);
    return {
        ctx,
        toasts,
        changed,
        calls,
        evaluate,
        restore: () => evaluate('_desktopRestoreContext()'),
        lost: (reason) => evaluate(`_desktopLocalLost(${JSON.stringify(reason)})`),
        refresh: () => evaluate('refreshWorkspaceSelector()'),
        // Read back through JSON so the values are this realm's objects: a
        // sandbox object has a different prototype and would compare unequal
        // even when it holds exactly the same fields.
        state: () => JSON.parse(JSON.stringify(evaluate(
            '({ context: _desktopContext, key: _desktopContextKey,'
            + ' chip: _wsSelState.current, recents: _wsSelState.recents,'
            + ' workspaceRoot: _wsSelState.defaultWorkspace })'))),
        seen: () => JSON.parse(JSON.stringify(calls)),
        seed: (code) => evaluate(code),
    };
}

test('a local project is resumed after a reload, from the host and not a cache', async () => {
    const page = loadConsolePage({
        host: {
            localContext: (params) => {
                page.calls.push(params);
                return Promise.resolve(LIVE);
            },
        },
    });
    await page.restore();
    const state = page.state();
    assert.deepEqual(page.seen(), [{ agent_id: 'agent_a', business_session_id: 'session_1' }]);
    assert.deepEqual(state.chip, { path: 'desktop:grant_1', name: 'reports' });
    assert.deepEqual(state.context, {
        binding_id: 'bind_1', workspace_id: 'ws_1', grant_version: 3,
    });
    assert.equal(state.key, 'agent_a\u0000session_1', 'the reference belongs to this Agent + chat');
    assert.deepEqual(page.changed, [true], 'the panel is told to follow the resumed source');
    assert.deepEqual(page.toasts, [], 'resuming is not an error');
});

test('a hostile answer cannot smuggle a path into the page', async () => {
    const page = loadConsolePage({
        host: {
            localContext: () => Promise.resolve({
                ...LIVE,
                abs_path: '/Users/someone/private',
                path: '/Users/someone/private',
                root: '/Users/someone',
            }),
        },
    });
    await page.restore();
    const state = page.state();
    assert.deepEqual(Object.keys(state.context).sort(),
        ['binding_id', 'grant_version', 'workspace_id']);
    assert.equal(JSON.stringify(state).includes('/Users/someone'), false,
        'no part of the answer that names a directory may be kept');
});

test('an expired local project is not resumed, and the user is told why', async () => {
    const page = loadConsolePage({
        host: { localContext: () => Promise.resolve({ state: 'stale', reason: 'grant_revoked' }) },
    });
    await page.restore();
    const state = page.state();
    assert.equal(state.context, null);
    assert.equal(state.chip, null, 'a cached name is not a project');
    assert.deepEqual(page.changed, []);
    assert.deepEqual(page.toasts, ['ws_sel_local_lost']);
});

test('a chat with no local project resumes nothing and says nothing', async () => {
    const page = loadConsolePage({
        host: { localContext: () => Promise.resolve({ state: 'none' }) },
    });
    await page.restore();
    assert.equal(page.state().context, null);
    assert.deepEqual(page.toasts, []);
});

test('an answer that arrives after the user moved on is dropped', async () => {
    let release = null;
    const page = loadConsolePage({
        host: {
            localContext: () => new Promise((resolve) => { release = () => resolve(LIVE); }),
        },
    });
    const pending = page.restore();
    // The user switches chat while the host is still answering.
    page.seed('sessionId = "session_2";');
    release();
    await pending;
    const state = page.state();
    assert.equal(state.context, null, 'the answer belongs to the chat that asked');
    assert.equal(state.chip, null);
    assert.deepEqual(page.changed, []);
});

test('a host that refuses, throws or is absent never breaks the page', async () => {
    const missing = loadConsolePage({ host: {} });
    await missing.restore();
    assert.equal(missing.state().context, null);

    const throwing = loadConsolePage({
        host: { localContext: () => Promise.reject(new Error('the host refused the call')) },
    });
    await throwing.restore();
    assert.equal(throwing.state().context, null);
    assert.deepEqual(throwing.toasts, [], 'a refusal is not a lost project');

    const none = loadConsolePage({});
    await none.restore();
    assert.equal(none.state().context, null);
});

test('losing the local project drops the reference and re-reads the selector', async () => {
    const page = loadConsolePage({ host: { localContext: () => Promise.resolve(LIVE) } });
    await page.restore();
    assert.equal(page.state().chip.path, 'desktop:grant_1');

    page.lost('stale_context');
    await settle();
    const state = page.state();
    assert.equal(state.context, null, 'the reference to a directory nothing can read is gone');
    assert.deepEqual(page.changed, [true, true], 'the panel re-derives its source');
    assert.deepEqual(page.toasts, ['ws_sel_local_lost']);
    assert.deepEqual(state.chip, { path: '/srv/proj', name: 'proj' },
        'the selector follows the live source instead of the revoked local name');

    page.lost('stale_context');
    await settle();
    assert.deepEqual(page.toasts, ['ws_sel_local_lost'], 'a repeated report is not repeated to the user');
});

test('the server project list does not wipe the local chip while it is in effect', async () => {
    const page = loadConsolePage({ host: { localContext: () => Promise.resolve(LIVE) } });
    await page.restore();
    // Opening the folder menu re-reads the server's projects; the server cannot
    // report a directory that lives on this machine.
    await page.refresh();
    const state = page.state();
    assert.deepEqual(state.chip, { path: 'desktop:grant_1', name: 'reports' });
    assert.equal(state.recents.length >= 0, true);
    assert.equal(page.state().context.workspace_id, 'ws_1');
});

test('without a local project the server keeps answering for the selector', async () => {
    const page = loadConsolePage({ host: {} });
    await page.refresh();
    assert.deepEqual(page.state().chip, { path: '/srv/proj', name: 'proj' });
});

// ---------------------------------------------------------------------------
// The panel: the end of a watch and a refused read
// ---------------------------------------------------------------------------

/**
 * The panel's change handler, in a sandbox.
 *
 * `_desktopLocalLost` is a stub here on purpose: the console owns what happens
 * to the reference, and this test is only about the panel handing the event on
 * with the right reason (the two halves are asserted separately above).
 */
function loadPanelPage() {
    const lost = [];
    const sourceChanged = [];
    const watchStops = [];
    const ctx = {
        console: { log() {}, warn() {}, error() {} },
        Promise, JSON, String, Number, Object, Array, encodeURIComponent,
        wsPanelOpen: false,
        wsCurrentDir: '',
        wsCurrentFile: null,
        _desktopLocalLost: (reason) => lost.push(String(reason)),
        wsSourceChanged: () => sourceChanged.push(true),
        _wsToast: () => undefined,
        t: (key) => key,
    };
    vm.createContext(ctx);
    vm.runInContext([
        'let wsWatchOff = null;',
        extractFunction(workspaceJs, 'wsWatchStop'),
        extractFunction(workspaceJs, 'wsOnProjectChanged'),
    ].join('\n'), ctx);
    return {
        ctx,
        lost,
        sourceChanged,
        watchStops,
        change: (event) => vm.runInContext(`wsOnProjectChanged(${JSON.stringify(event)})`, ctx),
        arm: () => vm.runInContext(
            'wsWatchOff = function () { globalThis.__stopped = true; };', ctx),
        stopped: () => vm.runInContext('!!globalThis.__stopped', ctx),
    };
}

test('the panel drops the local source when the host reports the watch ended', () => {
    const page = loadPanelPage();
    page.arm();
    page.change({ ended: true, reason: 'stale_context' });
    assert.deepEqual(page.lost, ['stale_context']);
    assert.deepEqual(page.sourceChanged, [], 'the console owns re-deriving the source');
    assert.equal(page.stopped(), true, 'the subscription is not left running');
});

test('a watch stopped for another reason just re-derives the source', () => {
    const page = loadPanelPage();
    page.change({ ended: true, reason: 'stopped' });
    assert.deepEqual(page.lost, []);
    assert.deepEqual(page.sourceChanged, [true]);
});

/** The panel's request seam, with the local source answering as a stub. */
function loadPanelApi(options = {}) {
    const lost = [];
    const requests = [];
    const ctx = {
        console: { log() {}, warn() {}, error() {} },
        Promise, JSON, String, Number, Object, Array, encodeURIComponent,
        sessionId: 'session_1',
        activeAgentId: 'agent_a',
        _desktopLocalLost: (reason) => lost.push(String(reason)),
        wsScopedAgentId: () => 'agent_a',
        CowProjectSource: { handle: async () => options.local },
        fetch: async (url) => {
            requests.push(url);
            return { ok: true, status: 200, json: async () => ({ status: 'success' }) };
        },
    };
    vm.createContext(ctx);
    vm.runInContext(extractFunction(workspaceJs, 'wsApi'), ctx);
    return {
        lost,
        requests,
        api: (p) => vm.runInContext(`wsApi(${JSON.stringify(p)})`, ctx),
    };
}

test('a local read refused as stale_context drops the reference too', async () => {
    const page = loadPanelApi({
        local: { status: 'error', code: 'stale_context', message: 'no local project is open' },
    });
    await assert.rejects(() => page.api('/api/workspace/tree?path='), (err) => {
        assert.equal(err.code, 'stale_context');
        assert.equal(err.local, true);
        return true;
    });
    assert.deepEqual(page.lost, ['stale_context']);
    assert.deepEqual(page.requests, [],
        'the panel does not quietly ask the server for a different file of the same name');
});

test('a local read refused for another reason keeps the project', async () => {
    const page = loadPanelApi({
        local: { status: 'error', code: 'source_read_only', message: 'this project is read-only' },
    });
    await assert.rejects(() => page.api('/api/workspace/write'), (err) => {
        assert.equal(err.code, 'source_read_only');
        return true;
    });
    assert.deepEqual(page.lost, []);
});

// ---------------------------------------------------------------------------
// The page-side host adapter
// ---------------------------------------------------------------------------

const adapterFactory = vm.runInThisContext(
    `(function (window) {\n${hostAdapterSource}\n})`,
    { filename: 'desktop-host.js' },
);

function loadAdapter(bridge) {
    const calls = [];
    const api = {
        getCapabilities: () => Promise.resolve({
            bridge: '1.0',
            methods: ['getCapabilities', 'suspendLocalContext', 'saveArtifact',
                'openExternal', 'onHostEvent', 'bindContext', 'chooseWorkspace',
                'disconnectWorkspace', 'localContext'],
            localFiles: true,
        }),
        suspendLocalContext: () => Promise.resolve({ suspended: true }),
        saveArtifact: () => Promise.resolve({ saved: true }),
        openExternal: () => Promise.resolve({ opened: true }),
        onHostEvent: () => () => undefined,
        bindContext: () => Promise.resolve({ ok: true }),
        chooseWorkspace: () => Promise.resolve({ activated: false }),
        disconnectWorkspace: () => Promise.resolve({ revoked: 0 }),
    };
    // Only a host that *has* the method gets one: `{}` stands for a host built
    // before this method existed, which must read as "nothing to resume".
    if (bridge && typeof bridge.localContext === 'function') {
        api.localContext = (params) => {
            calls.push(params);
            return bridge.localContext(params);
        };
    }
    const window = {
        addEventListener: () => undefined,
        setTimeout: (fn) => { void fn; return 0; },
        open: () => null,
        document: { body: { appendChild() {}, removeChild() {} }, createElement: () => ({}) },
        desktopHost: bridge ? api : undefined,
    };
    adapterFactory(window);
    return { host: window.CowDesktopHost, calls: () => JSON.parse(JSON.stringify(calls)) };
}

test('the page asks the host for the live context and forwards its answer', async () => {
    const { host, calls } = loadAdapter({
        localContext: () => Promise.resolve(LIVE),
    });
    const reply = await host.localContext({ agent_id: 'a', business_session_id: 's' });
    assert.deepEqual(calls(), [{ agent_id: 'a', business_session_id: 's' }]);
    assert.equal(reply.state, 'live');
    assert.equal(reply.workspace_id, 'ws_1');
});

test('a browser or an older host answers "nothing to resume", never a throw', async () => {
    const browser = loadAdapter(null);
    assert.deepEqual(await browser.host.localContext({}), { state: 'none', reason: 'no_host' });

    const older = loadAdapter({});
    // A host built before the method existed: `withBridge` finds a bridge, the
    // method is missing, and the answer is the same "nothing here".
    const reply = await older.host.localContext({});
    assert.equal(reply.state, 'none');
    // And a host that refuses is not allowed to become an exception.
    const refusing = loadAdapter({ localContext: () => Promise.reject(new Error('nope')) });
    const refused = await refusing.host.localContext({});
    assert.equal(refused.state, 'none');
});

// ---------------------------------------------------------------------------
// The host's resume decision
// ---------------------------------------------------------------------------

// The decision itself is pure, and this is where it is tested: everything above
// stubs the host, so without this the rule "resume only what is live and only
// what belongs to this chat" would have no test on the machine that enforces it.
let restoreLocalContext;

function needsBuild() {
    const out = path.join(DIST, 'project-browser', 'restore.js');
    return !fs.existsSync(out) || fs.statSync(out).mtimeMs < fs.statSync(RESTORE_TS).mtimeMs;
}

before(() => {
    if (needsBuild()) {
        execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: DESKTOP, stdio: 'pipe' });
    }
    restoreLocalContext = require(path.join(DIST, 'project-browser', 'restore.js'))
        .restoreLocalContext;
});

const RECORD = { workspaceId: 'ws_1', grantId: 'grant_1', bindingId: 'bind_1', agentId: 'agent_a', businessSessionId: 'session_1' };
const LIVE_GRANT = {
    workspaceId: 'ws_1', bindingId: 'bind_1', grantId: 'grant_1', grantVersion: 3,
    selectionGeneration: 2, label: 'reports', executable: true, connected: true,
};

test('a resumed project is re-read from the live registry, not from the record', () => {
    const answer = restoreLocalContext(
        { agentId: 'agent_a', businessSessionId: 'session_1' },
        [RECORD],
        (workspaceId) => (workspaceId === 'ws_1' ? LIVE_GRANT : null),
    );
    assert.deepEqual(answer, {
        state: 'live',
        binding: {
            workspace_id: 'ws_1', binding_id: 'bind_1', grant_id: 'grant_1',
            grant_version: 3, selection_generation: 2, label: 'reports',
            executable: true, connected: true,
        },
    });
});

test('a record whose grant is gone is stale, not an open project', () => {
    // The record outlives the grant it names: resuming from the record alone
    // would present a revoked directory as if nothing had happened.
    const answer = restoreLocalContext(
        { agentId: 'agent_a', businessSessionId: 'session_1' },
        [RECORD],
        () => null,
    );
    assert.deepEqual(answer, { state: 'stale', reason: 'grant_revoked' });
});

test('a re-picked project resumes at the version that is live now', () => {
    const answer = restoreLocalContext(
        { agentId: 'agent_a', businessSessionId: 'session_1' },
        [RECORD],
        () => ({ ...LIVE_GRANT, grantId: 'grant_2', grantVersion: 9, label: 'reports-v2' }),
    );
    assert.equal(answer.state, 'live');
    assert.equal(answer.binding.grant_id, 'grant_2');
    assert.equal(answer.binding.grant_version, 9, 'the page must not resume against grant_1');
    assert.equal(answer.binding.label, 'reports-v2');
});

test('another chat\'s confirmation is not resumed', () => {
    const other = { ...RECORD, agentId: 'agent_b' };
    const otherChat = { ...RECORD, businessSessionId: 'session_2' };
    for (const request of [
        { agentId: 'agent_a', businessSessionId: 'session_1' },
        { agentId: 'agent_a', businessSessionId: 'session_3' },
    ]) {
        assert.deepEqual(
            restoreLocalContext(request, [other, otherChat], () => LIVE_GRANT),
            { state: 'none' },
            `${request.agentId}/${request.businessSessionId} has no project of its own`,
        );
    }
});

test('the newest confirmation for a chat is the one in effect', () => {
    const older = { ...RECORD, workspaceId: 'ws_0', bindingId: 'bind_0' };
    const answer = restoreLocalContext(
        { agentId: 'agent_a', businessSessionId: 'session_1' },
        [older, RECORD],
        (workspaceId) => (workspaceId === 'ws_1' ? LIVE_GRANT : { ...LIVE_GRANT, workspaceId: 'ws_0' }),
    );
    assert.equal(answer.binding.workspace_id, 'ws_1');
});

test('the resumed answer names the project without naming a directory', () => {
    const answer = restoreLocalContext(
        { agentId: 'agent_a', businessSessionId: 'session_1' },
        [RECORD],
        () => ({ ...LIVE_GRANT, absolutePath: '/Users/someone/private', root: '/Users/someone' }),
    );
    assert.equal(answer.state, 'live');
    assert.deepEqual(Object.keys(answer.binding).sort(), [
        'binding_id', 'connected', 'executable', 'grant_id', 'grant_version',
        'label', 'selection_generation', 'workspace_id',
    ]);
});

// ---------------------------------------------------------------------------
// The notice the user gets
// ---------------------------------------------------------------------------

test('the lost-local-project notice exists in every language', () => {
    // The browser and the desktop console share one dictionary set, and the
    // notice is only reachable in the desktop one -- a missing string would ship
    // as the raw key, which is indistinguishable from a bug to the user.
    for (const expected of [
        /"ws_sel_local_lost": "本机项目授权已失效，已回到服务器工作区"/,
        /"ws_sel_local_lost": "本機專案授權已失效，已回到伺服器工作區"/,
        /"ws_sel_local_lost": "The local project is no longer authorized/,
    ]) {
        assert.match(i18nCore, expected);
    }
    for (const expected of [
        /ws_sel_local_lost: '本机项目授权已失效，已回到服务器工作区'/,
        /ws_sel_local_lost: '本機專案授權已失效，已回到伺服器工作區'/,
        /ws_sel_local_lost: 'The local project is no longer authorized/,
    ]) {
        assert.match(i18nJs, expected);
    }
});
