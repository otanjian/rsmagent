// Run the shipped sidebar-history handlers against a small DOM and controlled
// transport. Backend tests own the archive filter; these cases pin the sidebar
// affordance: one archive control per row, hiding without opening the session,
// rollback on failure, and restore from the archived dialog.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
const start = source.indexOf('// === SIDEBAR_RECENT_BEGIN ===');
const end = source.indexOf('// Never run sidebar init inline', start);
assert.ok(start >= 0 && end > start, 'Missing sidebar recent section in console.js');
const sidebarSource = source.slice(start, end);

function element(tagName = 'div') {
    const classes = new Set();
    let text = '', html = '';
    const el = {
        tagName: tagName.toUpperCase(), value: '', dataset: {}, attrs: {}, handlers: {},
        children: [], parentNode: null, disabled: false, hidden: false, type: '',
        get firstChild() { return this.children[0] || null; },
        get textContent() { return text; },
        set textContent(value) { text = String(value); html = ''; this.children = []; },
        get innerHTML() { return html; },
        set innerHTML(value) { html = String(value); text = ''; this.children = []; },
        get className() { return [...classes].join(' '); },
        set className(value) { classes.clear(); String(value).split(/\s+/).filter(Boolean).forEach(v => classes.add(v)); },
        classList: {
            add: (...names) => names.forEach(name => classes.add(name)),
            remove: (...names) => names.forEach(name => classes.delete(name)),
            contains: name => classes.has(name),
            toggle(name, force) {
                const add = force === undefined ? !classes.has(name) : force;
                if (add) classes.add(name); else classes.delete(name);
                return add;
            },
        },
        setAttribute(key, value) { this.attrs[key] = String(value); },
        getAttribute(key) { return this.attrs[key] ?? null; },
        removeAttribute(key) { delete this.attrs[key]; },
        addEventListener(name, fn) { this.handlers[name] = fn; },
        appendChild(child) {
            if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(el => el !== child);
            this.children.push(child); child.parentNode = this; return child;
        },
        insertBefore(child, ref) {
            if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(el => el !== child);
            const idx = ref ? this.children.indexOf(ref) : -1;
            if (idx < 0) this.children.push(child); else this.children.splice(idx, 0, child);
            child.parentNode = this; return child;
        },
        removeChild(child) {
            this.children = this.children.filter(el => el !== child); child.parentNode = null; return child;
        },
        remove() { if (this.parentNode) this.parentNode.removeChild(this); },
        focus() { this.focused = true; },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
        querySelectorAll(selector) {
            const matches = child => selector.startsWith('.')
                ? child.className.split(' ').includes(selector.slice(1))
                : child.tagName.toLowerCase() === selector.toLowerCase();
            return this.children.flatMap(child => [
                ...(matches(child) ? [child] : []), ...child.querySelectorAll(selector),
            ]);
        },
    };
    return el;
}

async function settle() {
    for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve));
}

function session(id, extra = {}) {
    return { session_id: id, title: id, last_active: 1700000000, pinned: 0,
        agent: { id: 'agent-a', name: 'Agent A' }, project: null, ...extra };
}
function payload(sessions, extra = {}) {
    return { ok: true, status: 200, json: async () => ({ status: 'success', sessions,
        total: sessions.length, has_more: false, group_mode: 'time', project_order: [], ...extra }) };
}

function setup(fetchImpl) {
    const nodes = new Map();
    const node = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
    const calls = [];
    const toasts = [];
    const switched = [];
    const body = element('body');
    const ctx = vm.createContext({
        console, Date, Intl, URLSearchParams, AbortController,
        activeAgentId: 'agent-a', sessionId: 'current',
        location: { pathname: '/chat' },
        _navAreaFromPath: () => 'workbench',
        window: { innerWidth: 1440 },
        document: {
            getElementById: node, createElement: element, body,
            querySelector: () => null, querySelectorAll: () => [],
        },
        localStorage: { getItem: () => null, setItem() {} },
        t: key => key, escapeHtml: value => String(value),
        switchSession: (sid, aid) => switched.push([sid, aid]),
        _wsToast: msg => toasts.push(msg),
        requestAnimationFrame: fn => fn(),
        setTimeout: (fn) => { fn(); return 0; }, clearTimeout() {},
        fetch(url, options) {
            calls.push({ url, options });
            return Promise.resolve(fetchImpl ? fetchImpl(url, options) : payload([]));
        },
    });
    const run = code => vm.runInContext(code, ctx);
    run(sidebarSource);
    const state = () => run('_sidebarRecentItems.map(s => s.session_id)');
    // ``_sidebarRecentItems`` is a script-level ``let``, so assigning the global
    // object property from Node would not be visible to the shipped code.
    const setItems = items => run(`_sidebarRecentItems = ${JSON.stringify(items)};`);
    return { ctx, run, node, calls, toasts, switched, state, setItems };
}

test('each sidebar row exposes an archive control that hides the session without opening it', async () => {
    const h = setup((url, options) => options && options.method === 'PUT'
        ? payload([])
        : payload([session('s2')]));
    h.setItems([session('s1'), session('s2')]);
    h.ctx.renderSidebarRecentSessions();

    const list = h.node('sidebar-recent-list');
    assert.equal(list.children.length, 2);
    const archiveBtn = list.children[0].querySelectorAll('.sidebar-recent-archive-btn')[0];
    assert.ok(archiveBtn, 'row has an archive control');

    archiveBtn.handlers.click({ stopPropagation() {} });
    await settle();

    assert.deepEqual([...h.state()], ['s2']);
    assert.deepEqual(h.switched, [], 'archiving must not open the session');
    const put = h.calls.find(c => c.options && c.options.method === 'PUT');
    assert.ok(put, 'archive writes through PUT');
    assert.equal(put.url, '/api/sessions/s1');
    assert.match(put.options.body, /"archived":true/);
    assert.ok(h.toasts.includes('session_archived'));
});

test('a failed archive restores the row and reports the reason', async () => {
    const h = setup(() => ({
        ok: false, status: 500,
        json: async () => ({ status: 'error', message: 'archive boom' }),
    }));
    h.setItems([session('s1'), session('s2')]);
    h.ctx.renderSidebarRecentSessions();

    const archiveBtn = h.node('sidebar-recent-list').children[0]
        .querySelectorAll('.sidebar-recent-archive-btn')[0];
    archiveBtn.handlers.click({ stopPropagation() {} });
    await settle();

    assert.deepEqual([...h.state()], ['s1', 's2'], 'failed archive leaves the list intact');
    assert.ok(h.toasts.includes('archive boom'));
    assert.deepEqual(h.switched, []);
});

test('the archived dialog lists archived sessions and restore writes archived=false', async () => {
    const h = setup(url => url.includes('archived=1')
        ? payload([session('gone', { title: 'Old chat' })])
        : payload([session('kept')]));
    h.ctx.openArchivedSessionsModal();
    await settle();

    const dialogBody = h.run('_archivedSessionsBody');
    const rows = dialogBody.querySelectorAll('.archived-session-row');
    assert.equal(rows.length, 1);
    assert.equal(rows[0].querySelectorAll('.archived-session-title')[0].textContent, 'Old chat');

    rows[0].querySelectorAll('.archived-session-restore')[0].handlers.click({});
    await settle();

    const put = h.calls.find(c => c.options && c.options.method === 'PUT');
    assert.ok(put, 'restore writes through PUT');
    assert.equal(put.url, '/api/sessions/gone');
    assert.match(put.options.body, /"archived":false/);
    assert.ok(h.toasts.includes('session_restored'));
});

test('an archived row shows its owning agent or workspace and last activity time', async () => {
    const h = setup(() => payload([session('gone', {
        title: 'Old chat', last_active: 1700000000,
        agent: { id: 'agent-a', name: 'Agent A' }, project: { name: 'Space' },
    })]));
    h.ctx.openArchivedSessionsModal();
    await settle();

    const row = h.run('_archivedSessionsBody').querySelectorAll('.archived-session-row')[0];
    const meta = row.querySelectorAll('.archived-session-meta')[0];
    const time = row.querySelectorAll('.archived-session-time')[0];
    assert.match(meta.textContent, /Agent A/);
    assert.match(meta.textContent, /Space/);
    assert.ok(time, 'archived row has a time element');
    assert.ok(time.textContent.length > 0, 'archived row shows the last activity time');
});

test('the archived dialog shows an empty state when nothing is archived', async () => {
    const h = setup(() => payload([]));
    h.ctx.openArchivedSessionsModal();
    await settle();

    const dialogBody = h.run('_archivedSessionsBody');
    assert.equal(dialogBody.querySelectorAll('.archived-session-row').length, 0);
    assert.equal(dialogBody.querySelectorAll('.archived-sessions-status')[0].textContent, 'archived_empty');
});

test('the archived dialog offers retry when the read fails', async () => {
    let fail = true;
    const h = setup(() => fail
        ? { ok: false, status: 500, json: async () => ({ status: 'error' }) }
        : payload([session('gone')]));
    h.ctx.openArchivedSessionsModal();
    await settle();

    const dialogBody = h.run('_archivedSessionsBody');
    const retry = dialogBody.querySelectorAll('.archived-session-retry')[0];
    assert.ok(retry, 'a failed read offers retry');

    fail = false;
    retry.handlers.click({});
    await settle();
    assert.equal(dialogBody.querySelectorAll('.archived-session-row').length, 1);
});
