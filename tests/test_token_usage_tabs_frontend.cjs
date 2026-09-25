// Token 消耗 tab switching (change add-audit-and-token-console).
//
// The page's three tabs (明细 / 按用户汇总 / 调用日志) are markup-driven: the
// module exposes `window.switchTokenUsageTab` exactly so the ported markup can
// call it from an inline `onclick`, the same way every other tab group in
// chat.html works (agent-detail-tab, config-tab, memory-tab, knowledge-tab).
//
// That contract has two halves and the defect this test exists to catch was
// that only one of them shipped: the function was defined and exported, the
// three panels and their loaders all existed, and the *buttons carried no
// handler at all* — so clicking 按用户汇总 or 调用日志 did nothing, while the
// 明细 panel stayed on screen. Nothing failed: the view loaded, the summary
// cards filled, the backend answered every panel shape
// (tests/test_audit_token_console_wire.py). A green suite could not tell the
// difference between "the tab works" and "the tab is dead", because no test
// ever asked whether pressing it reaches the switch.
//
// So the assertions come in two layers:
//   1. reachability — every tab button in the markup names its own id in a
//      handler, and the module exports the function that handler calls;
//   2. behaviour — invoking the switch moves the visible panel, moves the
//      active-tab styling, and refetches the panel that tab is about.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const CHAT_HTML = path.join(ROOT, 'channel/web/chat.html');
const MODULE = path.join(ROOT, 'channel/web/static/js/audit-console.js');

const TAB_IDS = ['details', 'by-user', 'call-logs'];

const html = fs.readFileSync(CHAT_HTML, 'utf8');
const moduleSource = fs.readFileSync(MODULE, 'utf8');

// --- fixtures ---------------------------------------------------------------

const RESPONSES = {
    summary: { summary: { total_prompt_tokens: 1500, total_completion_tokens: 300, total_calls: 7 } },
    details: { details: [{ date: '2026-09-25', tenant: 'tnt_acme', model: 'deepseek-chat', provider: 'deepseek', prompt_tokens: 100, completion_tokens: 20, call_count: 1 }] },
    'by-user': { users: [{ actor: 'alice', prompt_tokens: 100, completion_tokens: 20, call_count: 1 }] },
    call_logs: { logs: [{ created_at: 1790332792, tenant: 'tnt_acme', actor: 'alice', model: 'deepseek-chat', input_summary: 'hi', output_summary: 'hello', prompt_tokens: 100, completion_tokens: 20, status: 'success', duration_ms: 42, call_count: 1 }] },
    actors: { actors: [{ name: 'alice' }] },
};

class ClassList {
    constructor(initial) { this.set = new Set(initial ? initial.split(/\s+/).filter(Boolean) : []); }
    contains(name) { return this.set.has(name); }
    toggle(name, force) {
        const on = force === undefined ? !this.set.has(name) : !!force;
        if (on) this.set.add(name); else this.set.delete(name);
        return on;
    }
}

class El {
    constructor(id, attrs) {
        this.id = id;
        this.attrs = attrs || {};
        this.classList = new ClassList(this.attrs.class);
        this.innerHTML = '';
        this.textContent = '';
        this.value = '';
        this.style = {};
        this.listeners = {};
    }
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
    addEventListener(type, fn) { (this.listeners[type] || (this.listeners[type] = [])).push(fn); }
    querySelectorAll() { return []; }
}

// The three tab buttons, exactly as the markup declares them.
function parseButtons() {
    return [...html.matchAll(/<button[^>]*data-token-usage-tab="([^"]+)"[^>]*>/g)].map(m => {
        const id = m[1];
        const cls = /class="([^"]*)"/.exec(m[0]);
        return { id, class: cls ? cls[1] : '', tag: m[0] };
    });
}

// The panel the markup ships visible, and the ones it ships hidden. Read from
// chat.html rather than assumed: "明细 starts selected" is a claim about the
// markup, and the module's switch only has to keep agreeing with it.
function parsePanels() {
    const out = {};
    for (const m of html.matchAll(/<div[^>]*id="token-usage-([a-z-]+)-panel"[^>]*>/g)) {
        const cls = /class="([^"]*)"/.exec(m[0]);
        out[m[1]] = cls ? cls[1] : '';
    }
    return out;
}

const BUTTON_SPECS = parseButtons();
const PANEL_SPECS = parsePanels();

function buildContext() {
    const registry = new Map();
    const requests = [];
    const tabButtons = BUTTON_SPECS.map(s => new El('tab-' + s.id, { 'data-token-usage-tab': s.id, class: s.class }));
    for (const [name, cls] of Object.entries(PANEL_SPECS)) {
        registry.set('token-usage-' + name + '-panel', new El('token-usage-' + name + '-panel', { class: cls }));
    }
    for (const id of ['token-usage-table-body', 'token-usage-by-user-body',
        'token-usage-call-logs-body', 'token-usage-status', 'token-usage-prompt',
        'token-usage-completion', 'token-usage-calls', 'token-usage-model',
        'token-usage-actor', 'token-usage-start', 'token-usage-end']) {
        registry.set(id, new El(id, {}));
    }

    async function fetchStub(url) {
        requests.push(url);
        const type = (/(?:[?&])type=([^&]*)/.exec(url) || [])[1];
        const body = RESPONSES[type] || {};
        return { ok: true, status: 200, json: async () => body };
    }

    const documentStub = {
        getElementById: id => {
            if (!registry.has(id)) registry.set(id, new El(id, {}));
            return registry.get(id);
        },
        querySelectorAll: sel => (/token-usage-tab-btn/.test(sel) ? tabButtons : []),
        addEventListener: () => {},
    };

    const windowStub = {
        t: key => key,
        registerConsoleView: () => {},
    };
    windowStub.window = windowStub;

    const ctx = {
        window: windowStub,
        document: documentStub,
        fetch: fetchStub,
        console: { warn: () => {}, error: () => {}, log: () => {} },
        setTimeout,
    };
    vm.createContext(ctx);
    vm.runInContext(moduleSource, ctx, { filename: 'audit-console.js' });

    return { ctx, registry, requests, window: windowStub, tabButtons };
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

function panelVisibility(registry) {
    const out = {};
    for (const id of TAB_IDS) out[id] = !registry.get('token-usage-' + id + '-panel').classList.contains('hidden');
    return out;
}

// --- 1. reachability -------------------------------------------------------

test('every Token 消耗 tab button calls the switch the module exports', () => {
    assert.equal(BUTTON_SPECS.length, TAB_IDS.length,
        `chat.html declares ${TAB_IDS.length} token-usage tabs`);

    assert.equal(typeof buildContext().window.switchTokenUsageTab, 'function',
        'audit-console.js exports window.switchTokenUsageTab for the markup to call');

    for (const spec of BUTTON_SPECS) {
        assert.ok(TAB_IDS.includes(spec.id), `unexpected tab id in markup: ${spec.id}`);
        assert.ok(spec.tag.includes(`switchTokenUsageTab('${spec.id}')`),
            `the ${spec.id} tab must call switchTokenUsageTab('${spec.id}') — a tab with no handler `
            + 'renders, loads and answers, but does nothing when pressed');
    }
});

test('the markup tabs and the panels the module toggles are the same set', () => {
    // Guards the other direction: a tab added to the markup without a panel (or
    // a panel renamed out from under its tab) is invisible dead weight either way.
    const body = moduleSource.slice(
        moduleSource.indexOf('function switchTokenUsageTab'),
        moduleSource.indexOf('function switchTokenUsageTab') + 600);
    const list = /\[((?:\s*'[a-z-]+'\s*,?)+)\]/.exec(body);
    assert.ok(list, 'switchTokenUsageTab declares the panels it toggles');
    const panels = [...list[1].matchAll(/'([a-z-]+)'/g)].map(m => m[1]);

    assert.deepEqual(panels.slice().sort(), TAB_IDS.slice().sort());
    assert.deepEqual(
        BUTTON_SPECS.map(b => b.id).sort(),
        panels.slice().sort(),
        'every markup tab has a panel and every panel has a tab');
});

// --- 2. behaviour ----------------------------------------------------------

test('the 按用户汇总 tab shows its own panel and retires the others', async () => {
    const { registry, requests, window } = buildContext();
    assert.deepEqual(panelVisibility(registry),
        { details: true, 'by-user': false, 'call-logs': false },
        '明细 is the panel the markup ships visible');

    window.switchTokenUsageTab('by-user');
    assert.deepEqual(panelVisibility(registry),
        { details: false, 'by-user': true, 'call-logs': false },
        'exactly one panel is visible after the switch');

    await flush(); await flush();

    assert.ok(requests.some(u => /[?&]type=by-user/.test(u)),
        `the by-user panel was fetched: ${requests.join(' | ')}`);
    assert.match(registry.get('token-usage-by-user-body').innerHTML, /alice/,
        'the by-user table is filled from the by-user payload, not the details one');
});

test('the 调用日志 tab shows its own panel and retires the others', async () => {
    const { registry, requests, window } = buildContext();

    window.switchTokenUsageTab('call-logs');
    assert.deepEqual(panelVisibility(registry),
        { details: false, 'by-user': false, 'call-logs': true });

    await flush(); await flush();

    assert.ok(requests.some(u => /[?&]type=call_logs/.test(u)),
        `the call-log panel was fetched: ${requests.join(' | ')}`);
    const html_ = registry.get('token-usage-call-logs-body').innerHTML;
    assert.match(html_, /success/, 'the call-log row carries its status');
    assert.match(html_, /42/, 'the call-log row carries its duration');
});

test('the active-tab styling follows the selection', async () => {
    // The click has to be visible even before the fetch lands, so assert on the
    // class move rather than only on the panel: a switch that repaints the panel
    // but leaves 明细 looking selected reads as "the click did nothing".
    const { window, tabButtons } = buildContext();
    const byId = id => tabButtons.find(b => b.getAttribute('data-token-usage-tab') === id);

    assert.ok(byId('details').classList.contains('bg-white'), '明细 starts selected');
    assert.ok(!byId('by-user').classList.contains('bg-white'), '按用户汇总 starts unselected');

    window.switchTokenUsageTab('by-user');
    assert.ok(byId('by-user').classList.contains('bg-white'), 'the pressed tab becomes selected');
    assert.ok(!byId('details').classList.contains('bg-white'), 'the previous tab gives it up');

    window.switchTokenUsageTab('details');
    assert.ok(byId('details').classList.contains('bg-white'), 'switching back selects 明细 again');
    assert.ok(!byId('by-user').classList.contains('bg-white'));
});

test('switching tabs re-reads the panel each time, never showing a stale table', async () => {
    const { registry, requests, window } = buildContext();

    window.switchTokenUsageTab('by-user');
    await flush(); await flush();
    const afterFirst = requests.length;

    window.switchTokenUsageTab('call-logs');
    await flush(); await flush();

    assert.ok(requests.length > afterFirst, 'the second switch issued its own read');
    assert.deepEqual(panelVisibility(registry),
        { details: false, 'by-user': false, 'call-logs': true });
});
