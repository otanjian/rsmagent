// Exercise the shipped console functions with controlled transport and DOM.
// Browser acceptance separately verifies layout; these cases cover races that
// source-string assertions and a happy-path screenshot cannot detect.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
function section(start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from);
    assert.ok(from >= 0 && to > from, `Missing console section ${start}`);
    return source.slice(from, to);
}
function element() {
    const classes = new Set(['opacity-0']);
    return {
        textContent: '', innerHTML: '', dataset: {}, attrs: {},
        classList: {
            add: (...xs) => xs.forEach(x => classes.add(x)),
            remove: (...xs) => xs.forEach(x => classes.delete(x)),
            contains: x => classes.has(x),
            toggle: (x, on) => on ? classes.add(x) : classes.delete(x),
        },
        setAttribute(k, v) { this.attrs[k] = v; },
        removeAttribute(k) { delete this.attrs[k]; },
        querySelector: () => null,
        focus() { this.focused = true; },
    };
}
const agent = (id, extra = {}) => ({ id, name: id, avatar: null, description: '',
    is_default: false, can_chat: true, unavailable_reason: null, ...extra });
/** The coding-type badge markup inside ``html``, or null when there is none. */
function codingHint(html) {
    const found = html.match(/<span class="coding-agent-badge[^"]*"[^>]*>[\s\S]*?<\/span>/);
    return found ? found[0] : null;
}
const response = agents => ({ ok: true, json: async () => ({ status: 'success', agents }) });
const filterAgents = () => [
    agent('default', { name: '智能办公助理', is_default: true }),
    agent('finance', { name: '对账分析', description: '核对账款', tags: ['财务', '经营分析', '财务', ' '] }),
    agent('sales', { name: '客户对账', position: '商务专员', category: '销售服务', tags: ['销售', '经营分析'] }),
    agent('sap', { name: 'SAP 助手', tags: ['ERP'] }),
];
function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

function setup(fetchImpl = async () => response([agent('B')])) {
    const nodes = new Map();
    const events = [];
    const storage = new Map();
    // Capture rather than print: several cases deliberately drive the failure
    // path, and a test run must not look like it reported errors itself.
    const logs = [];
    const record = kind => (...parts) => logs.push(`${kind} ${parts.map(String).join(' ')}`);
    const silentConsole = { log: record('log'), info: record('info'), warn: record('warn'),
        error: record('error'), debug: record('debug') };
    const ctx = {
        console: silentConsole, Date, activeAgentId: 'A', sessionId: 'old-session', currentView: 'agent-workbench',
        agentNavigationVersion: 0, defaultAgentId: 'A', avatarVersions: {},
        agentCatalog: [{ ...agent('A'), enabled: true }],
        _identityMode: () => 'legacy', sessionStorage: { getItem: k => storage.get(k) || '' },
        localStorage: { setItem: (k, v) => storage.set(k, v) },
        writeScopedPreference: (k, v) => storage.set(k, v),
        document: { getElementById(id) { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); } },
        t: key => key, escapeHtml: value => String(value),
        agentAvatarHTML: a => `<avatar>${a.name}</avatar>`,
        fetch: (...args) => { events.push(['fetch', args[0]]); return fetchImpl(...args); },
        wsGuardUnsaved: () => true,
        newChat: (...args) => { events.push(['newChat', ...args]); ctx.sessionId = 'new-session'; },
        resetWorkspaceToAgentRoot: () => events.push(['workspace-root']),
        requestAnimationFrame: fn => fn(), multiAgentMode: () => true,
        currentTeamIds: () => [], conversationHasMessages: () => false,
        _renderModelChip: () => {}, _wsSelUpdateLabel: () => {},
        showConfirmDialog: options => events.push(['notice', options.message]),
    };
    ctx.findAgent = id => ctx.agentCatalog.find(a => a.id === id);
    ctx.navigateTo = view => { ctx.currentView = view; ctx.agentNavigationVersion++; ctx.cancelAgentStart(); };
    vm.createContext(ctx);
    vm.runInContext(section('let agentWorkbench =', 'function openAgentDetail'), ctx);
    vm.runInContext(section('let _agentStartInFlight =', 'function conversationHasMessages'), ctx);
    vm.runInContext(section('function renderComposerIdentity()', 'function toggleComposerAgentMenu'), ctx);
    vm.runInContext(section('function pickComposerAgent(', 'function inviteTeamMember('), ctx);
    vm.runInContext(section('async function refreshSessionSettings()', 'function _renderModelChip'), ctx);
    vm.runInContext(section('async function refreshWorkspaceSelector()', '// Sync the selector button'), ctx);
    vm.runInContext(`let _sessCfg = null; let _wsSelState = {};
        this.state = () => ({agents: agentWorkbench, loading: agentWorkbenchLoading,
            error: _wbLoadedError, notice: _wbNoticeKey, busy: !!_agentStartInFlight,
            emptyReason: _wbEmptyReason,
            settings: _sessCfg, workspace: _wsSelState});`, ctx);
    return { ctx, nodes, events, storage, logs, node: id => ctx.document.getElementById(id) };
}

test('keyword/tag intersection, per-agent facet counts and legacy untagged records', () => {
    const { ctx } = setup();
    const rows = filterAgents();
    const result = ctx.agentWorkbenchFilterResult([...rows, rows[1]], ' 对账 ', '财务');
    assert.deepEqual(Array.from(result.agents, a => a.id), ['finance']);
    const counts = new Map(result.options.map(o => [o.tag, o.count]));
    assert.equal(counts.get(null), 2);
    assert.equal(counts.get('财务'), 1);
    assert.equal(counts.get('经营分析'), 2);
    assert.equal(counts.get('ERP'), 0);
    assert.deepEqual(Array.from(ctx.agentWorkbenchFilterResult(rows, '', '').agents, a => a.id), ['default']);
    assert.deepEqual(Array.from(ctx.agentWorkbenchFilterResult(rows, ' SaP ', null).agents, a => a.id), ['sap']);
    for (const word of ['sales', '商务专员', '销售服务']) {
        assert.equal(ctx.agentWorkbenchFilterResult(rows, word, null).agents[0].id, 'sales');
    }
});

test('search and tag selection only narrow cards, not the shared chat roster or owner', async () => {
    const { ctx, node } = setup(async () => response(filterAgents()));
    await ctx.loadAgentWorkbench();
    ctx.onAgentWorkbenchSearch({ target: { value: '对账' } });
    vm.runInContext("selectAgentWorkbenchTag(_wbFilterOptions.findIndex(o => o.tag === '财务'))", ctx);
    assert.match(node('agent-workbench-grid').innerHTML, /data-agent-id="finance"/);
    assert.doesNotMatch(node('agent-workbench-grid').innerHTML, /data-agent-id="sales"/);
    assert.equal(ctx.state().agents.length, 4);
    assert.equal(ctx.chatAgentCatalog.length, 4);
    assert.equal(ctx.activeAgentId, 'A');
    assert.equal(ctx.sessionId, 'old-session');
    await ctx.loadAgentWorkbench(true);
    assert.doesNotMatch(node('agent-workbench-grid').innerHTML, /data-agent-id="sales"/);
    ctx.clearAgentWorkbenchSearch(true);
    assert.match(node('agent-workbench-grid').innerHTML, /data-agent-id="sales"/);
    assert.equal(node('agent-workbench-search').focused, true);
});

test('unmatched filters differ from an empty roster and a failed refresh', async () => {
    let fail = false;
    const { ctx, node } = setup(async () => {
        if (fail) return { ok: false, status: 403, json: async () => ({ code: 'forbidden' }) };
        return response(filterAgents());
    });
    await ctx.loadAgentWorkbench();
    ctx.onAgentWorkbenchSearch({ target: { value: '没有这个词' } });
    assert.match(node('agent-workbench-grid').innerHTML, /agent_workbench_no_match/);
    assert.equal(node('agent-workbench-results').hidden, false);
    assert.equal(node('agent-workbench-status').textContent, '');
    fail = true;
    await ctx.loadAgentWorkbench(true);
    assert.equal(node('agent-workbench-results').hidden, true);
    assert.equal(node('agent-workbench-filters').hidden, true);
    assert.match(node('agent-workbench-grid').innerHTML, /agent_workbench_no_permission/);
});

test('refresh removing the selected tag falls back to All while preserving the search', async () => {
    let rows = filterAgents();
    const { ctx, node } = setup(async () => response(rows));
    await ctx.loadAgentWorkbench();
    ctx.onAgentWorkbenchSearch({ target: { value: '对账' } });
    vm.runInContext("selectAgentWorkbenchTag(_wbFilterOptions.findIndex(o => o.tag === '财务'))", ctx);
    rows = rows.filter(a => a.id !== 'finance');
    await ctx.loadAgentWorkbench(true);
    assert.equal(vm.runInContext('_wbSelectedTag', ctx), null);
    assert.equal(vm.runInContext('_wbSearchQuery', ctx), '对账');
    assert.match(node('agent-workbench-grid').innerHTML, /data-agent-id="sales"/);
    assert.doesNotMatch(node('agent-workbench-grid').innerHTML, /data-agent-id="sap"/);
});

test('IME composition waits for committed input without replacing the search input', async () => {
    const { ctx, node } = setup(async () => response(filterAgents()));
    await ctx.loadAgentWorkbench();
    const input = node('agent-workbench-search');
    ctx.onAgentWorkbenchSearch({ target: { value: 'dui' }, isComposing: true });
    assert.match(node('agent-workbench-grid').innerHTML, /data-agent-id="default"/);
    ctx.onAgentWorkbenchSearch({ target: { value: '对账' } });
    assert.doesNotMatch(node('agent-workbench-grid').innerHTML, /data-agent-id="default"/);
    assert.equal(input, node('agent-workbench-search'));
});

test('identity changes clear previous filters and invalidate delayed list replies', async () => {
    const pending = deferred();
    let delay = false;
    const { ctx, node, storage } = setup(() => delay ? pending.promise : Promise.resolve(response(filterAgents())));
    ctx._authEpoch = 1;
    ctx._accountState = { username: 'alice' };
    await ctx.loadAgentWorkbench();
    ctx.onAgentWorkbenchSearch({ target: { value: '财务' } });
    delay = true;
    const old = ctx.loadAgentWorkbench();
    ctx._authEpoch++;
    pending.resolve(response([agent('old-secret', { tags: ['old-tag'] })]));
    await old;
    ctx.renderAgentWorkbench();
    assert.equal(ctx.state().agents.length, 0);
    assert.equal(vm.runInContext('_wbSearchQuery', ctx), '');
    assert.equal(node('agent-workbench-filters').hidden, true);
    delay = false;
    await ctx.loadAgentWorkbench();
    ctx.onAgentWorkbenchSearch({ target: { value: '对账' } });
    storage.set('cow_tenant_id', 'another-tenant');
    ctx.renderAgentWorkbench();
    assert.equal(ctx.chatAgentCatalog.length, 0);
    assert.equal(node('agent-workbench-grid').innerHTML, '');
    assert.equal(node('agent-workbench-tags').innerHTML, '');
});

test('a newer response uses the latest search and cannot resurrect old tags', async () => {
    const first = deferred(), second = deferred();
    let calls = 0;
    const { ctx, node } = setup(() => (++calls === 1 ? first : second).promise);
    const old = ctx.loadAgentWorkbench();
    const fresh = ctx.loadAgentWorkbench();
    ctx.onAgentWorkbenchSearch({ target: { value: 'SAP' } });
    second.resolve(response(filterAgents()));
    await fresh;
    first.resolve(response([agent('stale', { tags: ['stale-tag'] })]));
    await old;
    assert.match(node('agent-workbench-grid').innerHTML, /data-agent-id="sap"/);
    assert.doesNotMatch(node('agent-workbench-tags').innerHTML, /stale-tag/);
});

test('custom labels stay text, special tag names cannot collide with synthetic filters', async () => {
    const { ctx, node } = setup(async () => response([
        agent('safe', { tags: ['全部', '未打标签', '__proto__', '\"><img src=x onerror=alert(1)>'] }),
    ]));
    ctx.escapeHtml = text => String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    await ctx.loadAgentWorkbench();
    const html = node('agent-workbench-tags').innerHTML;
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&lt;img/);
    const result = ctx.agentWorkbenchFilterResult(ctx.state().agents, '', '全部');
    assert.equal(result.agents.length, 1);
    assert.equal(ctx.agentWorkbenchFilterResult(ctx.state().agents, '', '').agents.length, 0);
});

test('only the first tag row remains focusable when collapsed; expanding and resizing restore access', () => {
    const { ctx, node } = setup();
    const buttons = [0, 0, 40, 40, 80, 120].map(offsetTop => ({ offsetTop, hidden: false }));
    const list = node('agent-workbench-tags');
    list.clientWidth = 480;
    list.style = { setProperty() {} };
    list.querySelectorAll = () => buttons;
    ctx.layoutAgentWorkbenchTags();
    assert.deepEqual(buttons.map(b => b.hidden), [false, false, true, true, true, true]);
    assert.equal(node('agent-workbench-tags-toggle').hidden, false);
    ctx.toggleAgentWorkbenchTags();
    assert.ok(buttons.every(b => !b.hidden));
    assert.equal(node('agent-workbench-tags-toggle').attrs['aria-expanded'], 'true');
    buttons.forEach(b => { b.offsetTop = 0; });
    ctx.layoutAgentWorkbenchTags();
    assert.equal(node('agent-workbench-tags-toggle').hidden, true);
});

test('fresh workbench Agent starts even when absent from management cache', async () => {
    const { ctx, events, node } = setup(async () => response([agent('B', { name: 'New name' })]));
    await ctx.startChatWithAgent('B');
    assert.equal(ctx.activeAgentId, 'B');
    assert.equal(ctx.currentView, 'chat');
    assert.equal(events.filter(e => e[0] === 'newChat').length, 1);
    assert.deepEqual(events.find(e => e[0] === 'newChat'), ['newChat', true, false]);
    assert.equal(node('chat-agent-name').textContent, 'New name');
    assert.equal(node('chat-input').focused, true);
});

test('removed target is rejected despite stale enabled management entry', async () => {
    const { ctx, events, node } = setup(async () => response([]));
    ctx.agentCatalog.push({ ...agent('B'), enabled: true });
    await ctx.startChatWithAgent('B');
    assert.equal(ctx.activeAgentId, 'A');
    assert.equal(ctx.sessionId, 'old-session');
    assert.equal(events.filter(e => e[0] === 'newChat').length, 0);
    assert.equal(node('agent-workbench-status').textContent, 'agent_target_unavailable');
    assert.equal(node('agent-workbench-status').classList.contains('opacity-0'), false);
});

test('permission-denied target cannot launch and explains why', async () => {
    const { ctx, events, node } = setup(async () => response([agent('B', {
        can_chat: false, unavailable_reason: 'permission_denied',
    })]));
    await ctx.startChatWithAgent('B');
    assert.equal(events.filter(e => e[0] === 'newChat').length, 0);
    assert.match(node('agent-workbench-grid').innerHTML, /disabled aria-disabled="true"/);
    assert.match(node('agent-workbench-grid').innerHTML, /agent_permission_denied/);
});

test('double-click while validation is pending submits one session', async () => {
    const request = deferred();
    const { ctx, events } = setup(() => request.promise);
    const first = ctx.startChatWithAgent('B');
    await ctx.startChatWithAgent('B');
    assert.equal(events.filter(e => e[0] === 'fetch').length, 1);
    request.resolve(response([agent('B')]));
    await first;
    assert.equal(events.filter(e => e[0] === 'newChat').length, 1);
    assert.equal(ctx.state().busy, false);
});

test('navigation away and back discards a late start result', async () => {
    const request = deferred();
    const { ctx, events } = setup(() => request.promise);
    const start = ctx.startChatWithAgent('B');
    ctx.navigateTo('config');
    ctx.navigateTo('agent-workbench');
    request.resolve(response([agent('B')]));
    await start;
    assert.equal(ctx.activeAgentId, 'A');
    assert.equal(events.filter(e => e[0] === 'newChat').length, 0);
});

test('cancel before validation preserves all state', async () => {
    const { ctx, events } = setup();
    ctx.wsGuardUnsaved = () => false;
    await ctx.startChatWithAgent('B');
    assert.equal(events.length, 0);
    assert.equal(ctx.activeAgentId, 'A');
});

test('content becomes dirty while validating: do not partially switch', async () => {
    const request = deferred();
    const { ctx, events } = setup(() => request.promise);
    const start = ctx.startChatWithAgent('B');
    ctx.wsGuardUnsaved = () => false;
    request.resolve(response([agent('B')]));
    await start;
    assert.equal(ctx.activeAgentId, 'A');
    assert.equal(ctx.sessionId, 'old-session');
    assert.equal(events.filter(e => e[0] === 'newChat').length, 0);
});

test('network failure keeps context and allows retry', async () => {
    let failed = true;
    const { ctx, node } = setup(async () => { if (failed) throw Error('offline'); return response([agent('B')]); });
    await ctx.startChatWithAgent('B');
    assert.equal(ctx.activeAgentId, 'A');
    assert.equal(ctx.state().busy, false);
    assert.equal(node('agent-workbench-status').textContent, 'agent_start_failed');
    failed = false;
    await ctx.startChatWithAgent('B');
    assert.equal(ctx.activeAgentId, 'B');
});

test('newer list wins over an older response without changing owner', async () => {
    const a = deferred(), b = deferred(); let calls = 0;
    const { ctx } = setup(() => (++calls === 1 ? a.promise : b.promise));
    const first = ctx.loadAgentWorkbench();
    const second = ctx.loadAgentWorkbench(true);
    b.resolve(response([agent('new')])); await second;
    a.resolve(response([agent('old')])); await first;
    assert.equal(ctx.state().agents[0].id, 'new');
    assert.equal(ctx.activeAgentId, 'A');
});

test('tenant switch discards list response', async () => {
    const request = deferred();
    const { ctx, storage } = setup(() => request.promise);
    storage.set('cow_tenant_id', 'one');
    const load = ctx.loadAgentWorkbench();
    storage.set('cow_tenant_id', 'two');
    request.resolve(response([agent('private')])); await load;
    assert.equal(ctx.state().agents.length, 0);
});

test('empty state is visible and full management fallback is rejected', async () => {
    const { ctx, node } = setup(async () => response([]));
    await ctx.loadAgentWorkbench();
    assert.equal(node('agent-workbench-status').textContent, 'agent_workbench_empty');
    assert.equal(node('agent-workbench-status').classList.contains('opacity-0'), false);
    ctx.fetch = async () => ({ ok: true, json: async () => ({ status: 'success', agents: [], revision: 'old' }) });
    await ctx.loadAgentWorkbench();
    assert.equal(ctx.state().error, true);
    assert.match(node('agent-workbench-grid').innerHTML, /agent_workbench_retry/);
});

test('a transient read failure is retried once instead of showing a dead page', async () => {
    // A momentary transport or 5xx fault is exactly what a second attempt
    // repairs, so the page must not present it as a terminal「加载失败」before
    // trying again: that is the difference between a blip and a broken page.
    let calls = 0;
    const { ctx, node } = setup(async () => {
        calls++;
        if (calls === 1) throw Error('offline');
        return response([agent('B')]);
    });
    await ctx.loadAgentWorkbench();
    assert.equal(calls, 2, 'a transient read is retried once');
    assert.equal(ctx.state().error, false, 'a repaired read is not a failure state');
    assert.deepEqual(ctx.state().agents.map(a => a.id), ['B']);
    assert.doesNotMatch(node('agent-workbench-grid').innerHTML, /agent_workbench_retry/);
});

test('a persistent refusal names its cause instead of a generic failure', async () => {
    //「加载失败」cannot be acted on. A permission denial and a missing tenant
    // selection are recoverable, so the failure state must say which one it is
    // rather than sending the user hunting for a problem they cannot see.
    let calls = 0;
    const { ctx, node, logs } = setup(async () => {
        calls++;
        return { ok: false, status: 403, json: async () => ({ status: 'error', message: 'forbidden', code: 'forbidden' }) };
    });
    await ctx.loadAgentWorkbench();
    assert.equal(calls, 1, 'a deterministic denial is not retried');
    assert.equal(ctx.state().error, true);
    assert.equal(node('agent-workbench-status').textContent, 'agent_workbench_no_permission');
    assert.match(node('agent-workbench-grid').innerHTML, /agent_workbench_retry/);
    assert.ok(logs.some(line => /forbidden/.test(line)),
        'the underlying cause is logged so a report can be diagnosed');
});

test('an unselected tenant is reported as such, not as a generic failure', async () => {
    const { ctx, node } = setup(async () => ({
        ok: false, status: 400,
        json: async () => ({ status: 'error', message: 'tenant selection required', code: 'missing_tenant' }),
    }));
    await ctx.loadAgentWorkbench();
    assert.equal(node('agent-workbench-status').textContent, 'agent_workbench_no_tenant');
});

test('a render fault is not reported as a load failure', async () => {
    // The success path used to render inside the load chain's .then, so any
    // rendering exception was caught by the load handler and displayed as
    //「加载失败」. A UI defect therefore arrived as a dead read and could not be
    // told apart from a real one; keep the two failures distinguishable.
    const { ctx, node, logs } = setup(async () => response([agent('B')]));
    ctx.agentAvatarHTML = () => { throw Error('render exploded'); };
    await ctx.loadAgentWorkbench();
    assert.equal(ctx.state().error, false, 'the read succeeded, so it is not a load failure');
    assert.deepEqual(ctx.state().agents.map(a => a.id), ['B']);
    assert.notEqual(node('agent-workbench-status').textContent, 'agent_workbench_failed');
    assert.ok(logs.some(line => /render exploded/.test(line)), 'the render fault is logged');
});

test('a render fault while painting the loading state cannot escape the loader', async () => {
    // The first paint happens before the read resolves. A fault raised there
    // used to escape loadAgentWorkbench synchronously, so a caller awaiting the
    // returned promise got an exception instead of a rejection, and the fault
    // was neither logged nor kept out of the read's own result.
    const { ctx, logs } = setup(async () => response([agent('B')]));
    ctx.setWbStatus = () => { throw Error('paint exploded'); };
    let returned;
    try {
        returned = ctx.loadAgentWorkbench();
    } catch (err) {
        assert.fail(`a paint fault must not escape the loader: ${err.message}`);
    }
    assert.equal(typeof returned.then, 'function', 'the loader still returns its promise');
    await returned;
    assert.equal(ctx.state().error, false, 'a paint fault is not a load failure');
    assert.deepEqual(ctx.state().agents.map(a => a.id), ['B'], 'the read still applies');
    assert.ok(logs.some(line => /paint exploded/.test(line)), 'the paint fault is logged');
});

test('an empty list that hides Agents says so instead of "no Agents available"', async () => {
    //「暂无可用智能体」reports an authorization gap as a missing feature: the
    // Agents exist, the caller just cannot reach them. The server distinguishes
    // the two causes and the console must render them differently — while still
    // treating the read as a success (no failure banner, no retry, and no change
    // of the current Agent/session).
    const { ctx, node } = setup(async () => ({
        ok: true,
        json: async () => ({
            status: 'success', agents: [], empty_reason: 'no_reachable_agents',
        }),
    }));
    await ctx.loadAgentWorkbench();
    assert.equal(ctx.state().error, false, 'an empty list is a successful read');
    assert.equal(ctx.state().notice, '', 'an empty list is not a notice/error');
    assert.equal(node('agent-workbench-status').textContent,
        'agent_workbench_empty_unreachable');
    assert.equal(node('agent-workbench-status').classList.contains('opacity-0'), false);
    assert.doesNotMatch(node('agent-workbench-grid').innerHTML, /agent_workbench_retry/,
        'the empty state must not offer a retry');
    assert.equal(ctx.activeAgentId, 'A', 'the current Agent is untouched');
    assert.equal(ctx.sessionId, 'old-session', 'the session is untouched');
});

test('a tenant with no Agents keeps the plain empty message', async () => {
    const { ctx, node } = setup(async () => ({
        ok: true,
        json: async () => ({ status: 'success', agents: [], empty_reason: 'no_agents' }),
    }));
    await ctx.loadAgentWorkbench();
    assert.equal(ctx.state().error, false);
    assert.equal(node('agent-workbench-status').textContent, 'agent_workbench_empty');
});

test('an empty reason is cleared when a later read returns cards', async () => {
    // A stale reason must not label a populated list as unreachable.
    let empty = true;
    const { ctx, node } = setup(async () => ({
        ok: true,
        json: async () => (empty
            ? { status: 'success', agents: [], empty_reason: 'no_reachable_agents' }
            : { status: 'success', agents: [agent('B')], empty_reason: null }),
    }));
    await ctx.loadAgentWorkbench();
    empty = false;
    await ctx.loadAgentWorkbench(true);
    assert.deepEqual(ctx.state().agents.map(a => a.id), ['B']);
    assert.equal(ctx.state().emptyReason, '', 'a populated list carries no empty reason');
    assert.notEqual(node('agent-workbench-status').textContent,
        'agent_workbench_empty_unreachable');
});

test('an old backend with no reason code still shows the plain empty message', async () => {
    // `empty_reason` is additive: a server that predates it must not break the
    // page or borrow the unreachable wording.
    const { ctx, node } = setup(async () => response([]));
    await ctx.loadAgentWorkbench();
    assert.equal(ctx.state().error, false);
    assert.equal(node('agent-workbench-status').textContent, 'agent_workbench_empty');
});

test('default card leads even when its ID sorts last', async () => {
    const { ctx } = setup(async () => response([agent('aaa'), agent('zzz', { is_default: true })]));
    await ctx.loadAgentWorkbench();
    assert.equal(ctx.state().agents[0].id, 'zzz');
});

test('chat header follows composer changes, including single-Agent mode', () => {
    const { ctx, node } = setup();
    ctx.agentCatalog.push({ ...agent('B'), enabled: true });
    ctx.activeAgentId = 'B'; ctx.renderComposerIdentity();
    assert.equal(node('chat-agent-name').textContent, 'B');
    ctx.multiAgentMode = () => false;
    ctx.activeAgentId = 'A'; ctx.renderComposerIdentity();
    assert.equal(node('chat-agent-name').textContent, 'A');
    assert.equal(node('composer-identity').classList.contains('hidden'), true);
});

test('composer switch shares cancellation and fresh target validation', async () => {
    const { ctx, events } = setup(async () => response([]));
    ctx.currentView = 'chat';
    ctx.wsGuardUnsaved = () => false;
    await ctx.pickComposerAgent('B');
    assert.equal(ctx.activeAgentId, 'A');
    assert.equal(events.length, 0);
    ctx.wsGuardUnsaved = () => true;
    await ctx.pickComposerAgent('B');
    assert.equal(ctx.activeAgentId, 'A');
    assert.equal(ctx.sessionId, 'old-session');
    assert.ok(events.some(e => e[0] === 'notice' && e[1] === 'agent_target_unavailable'));
});

test('old session model and project responses cannot overwrite new context', async () => {
    const request = deferred();
    const { ctx } = setup(() => request.promise);
    const settings = ctx.refreshSessionSettings();
    const workspace = ctx.refreshWorkspaceSelector();
    ctx.activeAgentId = 'B'; ctx.sessionId = 'new-session';
    request.resolve({ ok: true, json: async () => ({ status: 'success', model: { id: 'old' }, current: { name: 'old-project' } }) });
    await Promise.all([settings, workspace]);
    assert.equal(ctx.state().settings, null);
    assert.equal(ctx.state().workspace.current, undefined);
});

test('the workbench read carries the Agent type through to the card', async () => {
    // The card case below hands the renderer a hand-built row, so it cannot see
    // whether the *read* keeps the type: this projection is normalised through a
    // field whitelist, and while ``agent_type`` was missing from that list every
    // row arrived as an ordinary Agent. The card, the new-chat row and
    // ``agentTypeOf``'s fallback all read the type off this roster, so the badge
    // and the surface routing were both starved of an answer the server had
    // already given. Asserted end to end -- read, then the painted grid -- so
    // either half alone cannot satisfy it.
    const { ctx, node } = setup(async () => response([
        agent('coder', { agent_type: 'coding' }),
        agent('plain', { agent_type: 'normal' }),
        agent('legacy'),
    ]));

    const agents = await ctx.fetchAgentWorkbench();
    assert.equal(agents.find(a => a.id === 'coder').agent_type, 'coding');
    assert.equal(agents.find(a => a.id === 'plain').agent_type, 'normal');
    // Absent stays absent rather than being asserted normal: "no type" and
    // "normal" have to remain distinguishable downstream (旧档案按 normal 处理).
    assert.equal(agents.find(a => a.id === 'legacy').agent_type, undefined);

    await ctx.loadAgentWorkbench();
    const grid = node('agent-workbench-grid').innerHTML;
    assert.equal(grid.split('coding-agent-badge').length - 1, 1,
        'exactly the coding Agent is marked');
    assert.match(grid, /agents_type_coding/);
});

test('the workbench card says when an Agent is a coding one', async () => {
    // The card's action opens the embedded OpenCode pane rather than the
    // platform composer, so the roster has to say which card does that: the
    // picker and the admin form already name the type, and a card that did not
    // would be the one place a coding Agent looked like an ordinary one.
    const { ctx } = setup(async () => response([]));

    const coding = ctx.agentWorkbenchCardHTML(
        { ...agent('coder', { agent_type: 'coding' }), enabled: true }, true, null);
    assert.match(coding, /coding-agent-badge/);
    assert.match(coding, /agents_type_coding/);

    // The hint is a glyph, not a label. As text it sat next to the Agent's own
    // name and read like a second name, so the wording moved into the tooltip
    // and the accessible name (change simplify-coding-agent-type-hint).
    const hint = codingHint(coding);
    assert.ok(hint, 'the coding card has no type hint');
    assert.match(hint, /<i class="fas fa-terminal"/);
    assert.doesNotMatch(hint, />[^<]*[^\s<]/,
        'the hint still renders visible text instead of only the icon');
    assert.match(hint, /role="img"/, 'the icon is not exposed as an image');
    assert.match(hint, /title="agents_type_coding"/, 'the hover hint lost the type wording');
    assert.match(hint, /aria-label="agents_type_coding"/, 'the icon has no accessible name');
    // One implementation: the picker row and the admin identity block draw the
    // same markup, so the three cannot drift into three different hints.
    assert.equal(hint, ctx.codingAgentTypeHint());

    // A normal Agent is not marked, so the badge keeps meaning something.
    const plain = ctx.agentWorkbenchCardHTML(
        { ...agent('plain', { agent_type: 'normal' }), enabled: true }, true, null);
    assert.doesNotMatch(plain, /coding-agent-badge/);

    // An Agent whose type the server did not project is treated as normal
    // (``opencode-coding-agents``: 缺少类型的旧档案 MUST 按 normal 处理).
    const legacy = ctx.agentWorkbenchCardHTML({ ...agent('old'), enabled: true }, true, null);
    assert.doesNotMatch(legacy, /coding-agent-badge/);
});

test('the icon hint is a square chip, not a text pill', () => {
    // The card's top-right column holds the default chip and the type hint
    // side by side, so the hint has to keep a fixed square: a pill that grows
    // with its label would squeeze the Agent's name again.
    const css = fs.readFileSync(
        path.join(__dirname, '../channel/web/static/css/coding.css'), 'utf8');
    const rule = css.match(/\.coding-agent-badge \{[\s\S]*?\}/);
    assert.ok(rule, 'the type hint has no styling rule');
    assert.match(rule[0], /width: 20px/);
    assert.match(rule[0], /height: 20px/);
    assert.match(rule[0], /justify-content: center/);
    assert.doesNotMatch(rule[0], /padding:/,
        'the chip still reserves room for a label it no longer has');
});
