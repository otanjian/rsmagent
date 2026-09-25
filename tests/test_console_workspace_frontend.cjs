// Verify the console workspace panel degrades readably when a request fails.
// Before this change the panel's routes were `closed` in database identity mode,
// so every tree/search/preview call answered `503 database_unavailable` and the
// panel rendered the raw backend string (or nothing at all). The panel must
// instead show a localized reason for the 503/403 cases and keep the backend
// message otherwise.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.join(__dirname, '../channel/web/static/js/workspace.js'), 'utf8');

function element(childCount = 0) {
    return {
        value: '',
        childElementCount: childCount,
        // The panel decides "this list has rows" from `childElementCount`, so
        // keep it in step with innerHTML the way the DOM does — otherwise a
        // cleared list would still look populated to the code under test.
        set innerHTML(html) {
            this._html = String(html);
            this.childElementCount = this._html.trim() ? 1 : 0;
        },
        get innerHTML() { return this._html || ''; },
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    };
}

/**
 * @param {object} payload - response for every request, unless `routes` overrides it.
 * @param {object} [routes] - payload by URL substring, so a test can answer the
 *   personal-Agent probe and the fallback tree differently from the refused one.
 */
function makeCtx(payload, routes = {}) {
    const nodes = new Map();
    const requests = [];
    const ctx = {
        wsPanelOpen: false,
        wsCurrentDir: '',
        wsCurrentRoot: '',
        wsSearchMode: false,
        currentLang: 'zh',
        t: key => key, // identity; assert on the raw key so it is locale-agnostic
        escapeHtml: x => String(x),
        localStorage: { getItem: () => null, setItem() {} },
        window: { addEventListener() {}, removeEventListener() {} },
        document: {
            getElementById: id => nodes.get(id) || null,
            querySelector: () => null,
            querySelectorAll: () => [],
            addEventListener() {},
            removeEventListener() {},
        },
        fetch: async (url) => {
            requests.push(url);
            const key = Object.keys(routes).find(k => url.includes(k));
            const body = key ? routes[key] : payload;
            return { ok: body.status === 'success', status: body.http || 200,
                json: async () => body };
        },
    };
    vm.createContext(ctx);
    // Everything before the "Init" section: definitions and literals only, so no
    // DOM wire-up runs and the functions can be called directly.
    const initIdx = source.indexOf('// Init');
    assert.ok(initIdx > 0, 'Init section not found');
    const cut = source.lastIndexOf('// =====', initIdx);
    vm.runInContext(source.slice(0, cut), ctx);
    return { ctx, nodes, requests, lastRequest: () => requests[requests.length - 1] };
}

test('wsErrorMessage localizes the database-unavailable 503', () => {
    const { ctx } = makeCtx({ status: 'success' });
    assert.equal(ctx.wsErrorMessage({ status: 503, code: 'database_unavailable',
        message: 'unavailable in database identity mode' }), 'ws_unavailable');
});

test('wsErrorMessage localizes forbidden 403 and keeps other messages', () => {
    const { ctx } = makeCtx({ status: 'success' });
    assert.equal(ctx.wsErrorMessage({ status: 403, code: 'forbidden', message: 'forbidden' }),
        'ws_forbidden');
    assert.equal(ctx.wsErrorMessage({ status: 404, code: 'not_found', message: 'boom' }), 'boom');
});

test('a database-unavailable tree renders the readable label, not the raw reason', async () => {
    const { ctx, nodes } = makeCtx({ status: 'error', code: 'database_unavailable', http: 503,
        message: 'unavailable in database identity mode' });
    nodes.set('ws-file-list', element());
    await ctx.loadWorkspaceDir('');
    const html = nodes.get('ws-file-list').innerHTML;
    assert.match(html, /ws_unavailable/);
    assert.doesNotMatch(html, /database_unavailable/);
    assert.doesNotMatch(html, /unavailable in database identity mode/);
});

test('a forbidden search renders the readable label', async () => {
    const { ctx, nodes } = makeCtx({ status: 'error', code: 'forbidden', http: 403,
        message: 'forbidden' });
    nodes.set('ws-file-list', element());
    await ctx.runWorkspaceSearch('report');
    const html = nodes.get('ws-file-list').innerHTML;
    assert.match(html, /ws_forbidden/);
    assert.ok(!html.includes('<span>forbidden</span>'), 'raw backend reason is not shown');
});

test('a successful tree still renders entries', async () => {
    const { ctx, nodes } = makeCtx({ status: 'success', path: '', root: '/ws/t1',
        entries: [{ name: 'a.txt', path: 'a.txt', is_dir: false, size: 3, kind: 'text' }] });
    nodes.set('ws-file-list', element());
    await ctx.loadWorkspaceDir('');
    const html = nodes.get('ws-file-list').innerHTML;
    assert.match(html, /a\.txt/);
});

// ---------------------------------------------------------------------------
// The panel button opens on the active Agent's own folders (the `agents/<id>`
// directory under the workspace root), and a refused Agent falls back to the
// caller's own private Agent.
// ---------------------------------------------------------------------------

const AGENT = 'rfq-quote';
const PERSONAL = { status: 'success', agents: [{ id: 'mine' }], default_agent_id: 'mine' };
const NO_PRIVATE = { status: 'success', agents: [], default_agent_id: '' };
const REFUSED = { status: 'error', code: 'forbidden', http: 403, message: 'forbidden' };
const MISSING = { status: 'error', http: 200, message: 'Not a directory: agents/x' };

// How the panel asks for a folder: the path is percent-encoded on the wire.
const dirQ = id => encodeURIComponent(`agents/${id}`);
const agentTree = id => ({ status: 'success', path: `agents/${id}`, root: '/ws/t1',
    entries: [{ name: 'knowledge', path: `agents/${id}/knowledge`, is_dir: true,
        size: 0, kind: 'directory' }] });
const ROOT_TREE = { status: 'success', path: '', root: '/ws/t1',
    entries: [{ name: 'users', path: 'users', is_dir: true, size: 0, kind: 'directory' }] };

// The panel's state lives in module-level `let` bindings, which are lexical:
// a property on the sandbox's global object is a *different* variable. Evaluate
// inside the context instead, so a test reads (and arranges) the same state the
// code does.
const peek = (ctx, name) => vm.runInContext(name, ctx);
const poke = (ctx, source) => vm.runInContext(source, ctx);
const flush = async () => {
    for (let i = 0; i < 5; i += 1) await new Promise(resolve => setImmediate(resolve));
};
const withAgent = (ctx, id = AGENT) =>
    poke(ctx, `activeAgentId = ${JSON.stringify(id)};`);

test('the panel button opens on the active Agent own folder', async () => {
    const { ctx, nodes, requests } = makeCtx(ROOT_TREE, { [dirQ(AGENT)]: agentTree(AGENT) });
    withAgent(ctx);
    nodes.set('ws-file-list', element(2));   // rows left by a previous folder
    nodes.set('workspace-panel', element());
    // A preview was open, and the browsed directory was another one.
    poke(ctx, 'wsCurrentFile = {path: "old.html"}; wsCurrentDir = "outputs";');
    ctx.toggleWorkspacePanel();
    await flush();
    assert.equal(peek(ctx, 'wsActiveTab'), 'files');
    assert.match(nodes.get('ws-file-list').innerHTML, /knowledge/);
    // One listing, asked for the Agent's folder — not the root, and not the
    // directory the previous visit had drilled into.
    assert.equal(requests.length, 1, requests.join('\n'));
    assert.match(requests[0], new RegExp(`path=${dirQ(AGENT)}`));
    assert.equal(peek(ctx, 'wsCurrentDir'), `agents/${AGENT}`);
});

test('a root without the Agent folder lands on the root itself', async () => {
    // A session that opened a project has no per-Agent folder under it; the
    // root is then the honest answer, not a "not a directory" error.
    const { ctx, nodes, requests } = makeCtx(ROOT_TREE, { [dirQ(AGENT)]: MISSING });
    withAgent(ctx);
    nodes.set('ws-file-list', element());
    await ctx.loadWorkspaceDir(`agents/${AGENT}`);
    assert.match(nodes.get('ws-file-list').innerHTML, /users/);
    assert.equal(peek(ctx, 'wsCurrentDir'), '');
    assert.equal(requests.length, 2, requests.join('\n'));
});

test('a missing directory the user navigated into is reported', async () => {
    // Only the landing path may quietly fall back to the root: browsing into a
    // directory that is gone is a real error.
    const { ctx, nodes, requests } = makeCtx(MISSING);
    withAgent(ctx);
    nodes.set('ws-file-list', element());
    await ctx.loadWorkspaceDir('outputs/gone');
    assert.match(nodes.get('ws-file-list').innerHTML, /Not a directory/);
    assert.equal(requests.length, 1, requests.join('\n'));
});

test('a refused Agent folder falls back to the caller own private Agent', async () => {
    const { ctx, nodes, requests } = makeCtx(REFUSED,
        { 'view=personal': PERSONAL, [dirQ('mine')]: agentTree('mine') });
    withAgent(ctx);
    nodes.set('ws-file-list', element());
    await ctx.loadWorkspaceDir(`agents/${AGENT}`);
    assert.match(nodes.get('ws-file-list').innerHTML, /knowledge/);
    assert.equal(requests.length, 3, requests.join('\n'));
    // The retry addresses the *fallback* Agent's own folder, which is a
    // different directory from the one that was refused.
    assert.match(requests[2], new RegExp(`path=${dirQ('mine')}`));
    // The fallback Agent stays in effect for the rest of the panel, otherwise
    // previewing a file there would be asked for under the refused Agent.
    assert.equal(peek(ctx, 'wsAgentOverride'), 'mine');
    assert.equal(peek(ctx, 'wsCurrentDir'), 'agents/mine');
});

test('the fallback is not retried once it is in effect', async () => {
    const { ctx, nodes, requests } = makeCtx(REFUSED,
        { 'view=personal': PERSONAL, [dirQ('mine')]: REFUSED });
    withAgent(ctx);
    nodes.set('ws-file-list', element());
    await ctx.loadWorkspaceDir(`agents/${AGENT}`);
    // The fallback Agent's folder is refused too, so the panel says so instead
    // of showing a folder it cannot vouch for.
    assert.match(nodes.get('ws-file-list').innerHTML, /ws_agent_forbidden/);
    // One refusal, one probe, one retry — a second fallback would loop.
    assert.equal(requests.filter(u => u.includes('view=personal')).length, 1,
        requests.join('\n'));
});

test('a member with no private Agent still sees the localized reason', async () => {
    const { ctx, nodes, requests } = makeCtx(REFUSED, { 'view=personal': NO_PRIVATE });
    withAgent(ctx);
    nodes.set('ws-file-list', element());
    await ctx.loadWorkspaceDir(`agents/${AGENT}`);
    // The landing, the probe, and the root candidate — all refused, and the
    // panel names the Agent folder in the reader's language rather than
    // reporting the API's own "agent not found" answer.
    assert.match(nodes.get('ws-file-list').innerHTML,
        /<span>ws_agent_forbidden<\/span>/);
    assert.equal(requests.length, 3, requests.join('\n'));
});

test('switching Agent drops the fallback and lands on the new Agent folder', async () => {
    const { ctx, nodes, requests } = makeCtx(agentTree('other'));
    nodes.set('ws-file-list', element());
    poke(ctx, "wsAgentOverride = 'mine'; wsCurrentDir = 'agents/mine'; "
        + "wsPanelOpen = true; activeAgentId = 'mine';");
    withAgent(ctx, 'other');
    ctx.resetWorkspaceToAgentRoot();
    await flush();
    assert.equal(peek(ctx, 'wsAgentOverride'), '');
    assert.equal(peek(ctx, 'wsCurrentDir'), 'agents/other');
    assert.equal(requests.length, 1, requests.join('\n'));
    assert.match(requests[0], new RegExp(`path=${dirQ('other')}`));
});

test('a session switch lands on the new session Agent folder', async () => {
    const { ctx, nodes, requests } = makeCtx(agentTree(AGENT));
    withAgent(ctx);
    nodes.set('ws-file-list', element());
    poke(ctx, "wsAgentOverride = 'mine'; wsPanelOpen = true; wsActiveTab = 'files';");
    ctx.wsOnSessionSwitch();
    await flush();
    assert.equal(peek(ctx, 'wsAgentOverride'), '');
    assert.equal(requests.length, 1, requests.join('\n'));
    assert.match(requests[0], new RegExp(`path=${dirQ(AGENT)}`));
});

// ---------------------------------------------------------------------------
// Stale responses: a read that started under the previous scope (Agent,
// session, account) must not paint into the new one. The list it names belongs
// to a folder the reader has left, and for an account switch to one they may
// not read at all.
// ---------------------------------------------------------------------------

/** Hold matching requests until `release()`, so a test controls arrival order. */
function gateFetch(ctx, match) {
    let release;
    const held = new Promise(resolve => { release = resolve; });
    const inner = ctx.fetch;
    ctx.fetch = async (url) => {
        if (!match || url.includes(match)) await held;
        return inner(url);
    };
    return release;
}

test('a listing that arrives after an Agent switch is dropped', async () => {
    const { ctx, nodes } = makeCtx(agentTree('new'), {
        [dirQ('old')]: agentTree('old'),
        [dirQ('new')]: agentTree('new'),
    });
    nodes.set('ws-file-list', element());
    // The panel is open, so the switch re-lists against the new Agent. Only the
    // *old* Agent's listing is held back, so it is guaranteed to arrive last —
    // exactly the order that would let it overwrite the new Agent's rows.
    poke(ctx, 'wsPanelOpen = true;');
    const release = gateFetch(ctx, dirQ('old'));

    const pending = ctx.loadWorkspaceDir('agents/old');
    withAgent(ctx, 'new');
    ctx.resetWorkspaceToAgentRoot();
    await flush();
    release();
    await pending;
    await flush();

    const html = nodes.get('ws-file-list').innerHTML;
    assert.match(html, /agents\/new\/knowledge/);
    assert.ok(!html.includes('agents/old/knowledge'),
        'the previous Agent directory was rendered into the new scope');
});

test('a search hit list that arrives after a session switch is dropped', async () => {
    const { ctx, nodes } = makeCtx({
        status: 'success',
        results: [{ name: 'stale.txt', path: 'agents/old/stale.txt',
                    is_dir: false, size: 1, kind: 'text' }],
    });
    nodes.set('ws-file-list', element());
    const release = gateFetch(ctx);

    const pending = ctx.runWorkspaceSearch('stale');
    ctx.wsOnSessionSwitch();
    release();
    await pending;
    await flush();

    assert.ok(!nodes.get('ws-file-list').innerHTML.includes('stale.txt'),
        'hits from the previous session were rendered');
});

test('closing the panel invalidates a listing still in flight', async () => {
    const { ctx, nodes } = makeCtx({ status: 'success', path: '', root: '/ws/t1',
        entries: [{ name: 'a.txt', path: 'a.txt', is_dir: false, size: 3,
                    kind: 'text' }] });
    nodes.set('ws-file-list', element());
    nodes.set('workspace-panel', element());
    const release = gateFetch(ctx);

    const pending = ctx.loadWorkspaceDir('');
    ctx.closeWorkspacePanel(true);
    release();
    await pending;
    await flush();

    assert.ok(!nodes.get('ws-file-list').innerHTML.includes('a.txt'),
        'a listing from the closed visit painted the panel');
});
