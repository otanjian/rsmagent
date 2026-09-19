// Verify the browser "Tasks" view does not hang on the hardcoded "Loading..."
// placeholder when the scheduler consumer is closed (e.g. database identity
// mode returns 503 "unavailable in database identity mode"). It should surface
// a readable reason and stop spinning instead.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');

function element() {
    const classes = new Set(['hidden']);
    const el = {
        innerHTML: '', textContent: '', dataset: {}, children: [],
        classList: {
            add: (...x) => x.forEach(c => classes.add(c)),
            remove: (...x) => x.forEach(c => classes.delete(c)),
            contains: c => classes.has(c),
            toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
        },
        setAttribute() {}, removeAttribute() {},
        querySelector(sel) { return (el._sel || {})[sel] || null; },
        appendChild(child) { el.children.push(child); return child; },
        addEventListener() {},
        style: {}, closest() { return null; }, focus() {},
    };
    return el;
}

// The tasks-empty element has a <p> placeholder; give the VM a real child to
// write textContent into so assertions see the replaced text.
function seededEl(extra) {
    const el = element();
    el.querySelector = sel => (sel === 'p' ? (el._p ||= element()) : el.querySelector(sel));
    Object.assign(el, extra || {});
    return el;
}

// The list element (tasks-list) is toggled hidden; the empty element owns the
// "Loading..." paragraph we assert against.
function setup(payload) {
    const nodes = new Map();
    const ctx = {
        agentCatalog: [], currentLang: 'zh',
        t: key => key, // identity; assert on the raw key so it is locale-agnostic
        loadAgentCatalog: async () => { ctx.agentCatalog = [{ id: 'owner', name: 'Owner' }]; },
        escapeHtml: x => String(x),
        findAgent: () => null,
        multiAgentMode: () => false,
        agentAvatarHTML: () => '',
        openTaskEditModal: () => {},
        showConfirmDialog: () => {},
        document: {
            getElementById(id) {
                if (!nodes.has(id)) nodes.set(id, id === 'tasks-empty' ? seededEl() : element());
                return nodes.get(id);
            },
            createElement(tag) {
                const el = element();
                el.tagName = tag;
                el.addEventListener = () => {};
                el.querySelector = () => (el._q ||= element());
                return el;
            },
        },
        fetch: async () => ({ json: async () => payload }),
        // console.js is a browser script: it reaches for `window` to find the
        // feature modules loaded ahead of it. The slice under test mounts the
        // scheduler console, which is a no-op when the module is absent -- so an
        // empty window is the honest sandbox here, not a stub to work around.
        window: {},
    };
    vm.createContext(ctx);
    // Slice includes `let tasksLoaded` and the loadTasksView body. loadAgentCatalog
    // is stubbed above; escapeHtml is not exercised for the success assertion.
    const from = source.indexOf('let tasksLoaded = false;');
    const to = source.indexOf('// =====================================================================\n// Logs View');
    assert.ok(from >= 0 && to > from, 'scheduler section not found');
    vm.runInContext(source.slice(from, to), ctx);
    return { ctx, get: id => ctx.document.getElementById(id) };
}

test('a closed scheduler consumer surfaces "tasks_unavailable" instead of hanging on Loading', async () => {
    const { ctx, get } = setup({
        status: 'error', message: 'unavailable in database identity mode', code: 'database_unavailable',
    });
    await ctx.loadTasksView();
    const empty = get('tasks-empty');
    const list = get('tasks-list');
    assert.equal(empty.classList.contains('hidden'), false, 'empty state becomes visible');
    assert.equal(empty.querySelector('p').textContent, 'tasks_unavailable', 'placeholder replaced with the unavailable label');
    assert.equal(list.classList.contains('hidden'), true, 'list stays hidden');
});

test('a non-closed error also replaces the Loading placeholder', async () => {
    const { ctx, get } = setup({ status: 'error', message: 'something else broke' });
    await ctx.loadTasksView();
    const empty = get('tasks-empty');
    assert.equal(empty.classList.contains('hidden'), false);
    assert.equal(empty.querySelector('p').textContent, 'something else broke', 'generic error message shown when not a closed consumer');
});

test('a successful non-empty list still renders and clears Loading', async () => {
    const { ctx, get } = setup({
        status: 'success',
        tasks: [{ id: 't1', name: 'nightly', enabled: true, agent_id: 'owner',
                  schedule: { type: 'cron', expression: '0 2 * * *' }, action: { content: 'run' } }],
    });
    await ctx.loadTasksView();
    const empty = get('tasks-empty');
    const list = get('tasks-list');
    assert.equal(empty.classList.contains('hidden'), true, 'empty state hidden');
    assert.equal(list.classList.contains('hidden'), false, 'list visible');
    const card = list.children[0];
    assert.ok(card, 'a task card is appended');
    assert.match(card.innerHTML, /nightly/, 'card renders the task name');
});
