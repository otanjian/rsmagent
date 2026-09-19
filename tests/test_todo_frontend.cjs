// Exercise the shipped TODO script through its public view entry points.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/todos.js'), 'utf8');
const messages = {
    todo_load_failed: '加载失败，请稍后重试。',
    todo_empty_open: '暂无未完成的待办',
    todo_disabled_banner: '待办功能未开启，可在配置中启用 todo_enabled。',
    todo_unauthorized_banner: '请先登录后再使用待办功能。',
    menu_todo: '我的待办',
};

function element(tag = 'div') {
    const classes = new Set();
    const attrs = new Map();
    const listeners = new Map();
    const el = {
        tagName: String(tag).toUpperCase(),
        id: '', type: '',
        textContent: '', innerHTML: '',
        parentNode: null,
        children: [],
        get className() { return [...classes].join(' '); },
        set className(value) {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach(name => classes.add(name));
        },
        classList: {
            add: (...names) => names.forEach(name => classes.add(name)),
            remove: (...names) => names.forEach(name => classes.delete(name)),
            contains: name => classes.has(name),
        },
        setAttribute(name, value) {
            attrs.set(name, String(value));
            if (name === 'id') el.id = String(value);
        },
        getAttribute(name) { return attrs.has(name) ? attrs.get(name) : null; },
        hasAttribute(name) { return attrs.has(name); },
        appendChild(child) { child.parentNode = el; el.children.push(child); return child; },
        insertBefore(child, ref) {
            child.parentNode = el;
            const at = el.children.indexOf(ref);
            if (at < 0) el.children.push(child);
            else el.children.splice(at, 0, child);
            return child;
        },
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(fn);
        },
        fire(type) { (listeners.get(type) || []).forEach(fn => fn()); },
        querySelector() { return null; },
        querySelectorAll() { return []; },
    };
    return el;
}

function response(data, status = 200) {
    return { ok: status >= 200 && status < 300, status, json: async () => data };
}

// Ids the top-bar bell creates for itself: they must not exist before the
// module mounts its entry, or the idempotency guard would skip mounting.
const BELL_IDS = new Set(['todo-bell-btn', 'todo-bell-badge']);

function setup(responses, options = {}) {
    const nodes = new Map();
    const creations = [];
    const absent = options.absent || new Set();
    const selectors = new Map();
    const focusListeners = [];
    const navigations = [];

    if (options.mountBell) {
        // The workbench header with the tenant selector as the anchor the bell
        // is inserted before.
        const header = element('header');
        const tenant = element('div');
        tenant.id = 'tenant-selector';
        header.appendChild(tenant);
        selectors.set('.workbench-header', header);
    }

    if (options.sidebarTodo) {
        // The removed sidebar entry used to carry its own count badge. Wire a
        // stand-in for it so a test can prove the module no longer paints it
        // (change remove-todo-sidebar-entry).
        const badge = element('span');
        badge.className = 'nav-badge hidden';
        selectors.set('.sidebar-item[data-view="todo"] .nav-badge', badge);
    }

    const node = id => {
        if (!nodes.has(id)) nodes.set(id, element());
        return nodes.get(id);
    };
    const walk = (el, id) => {
        if (!el) return null;
        if (el.id === id) return el;
        for (const child of el.children) {
            const hit = walk(child, id);
            if (hit) return hit;
        }
        return null;
    };
    const findById = id => {
        for (const root of selectors.values()) {
            const hit = walk(root, id);
            if (hit) return hit;
        }
        for (const el of creations) if (el.id === id) return el;
        return null;
    };
    const summaryReply = () => {
        const s = options.summary;
        if (typeof s === 'function') return s();
        return response(s || { status: 'success' });
    };

    const requests = [];
    const ctx = {
        URLSearchParams,
        I18N: { zh: messages },
        document: {
            readyState: 'complete',
            getElementById: id => {
                const hit = findById(id);
                if (hit) return hit;
                if (absent.has(id)) return null;
                return node(id);
            },
            querySelector: sel => selectors.get(sel) || null,
            querySelectorAll: () => [],
            addEventListener() {},
            createElement: tag => {
                const el = element(tag);
                creations.push(el);
                return el;
            },
        },
        addEventListener(type, fn) { if (type === 'focus') focusListeners.push(fn); },
        fetch: async (url, opts) => {
            requests.push({ url, options: opts });
            if (url === '/api/todos/summary') return summaryReply();
            assert.match(url, /^\/api\/todos\?/);
            assert.ok(responses.length, 'unexpected list request');
            const next = responses.shift();
            return typeof next === 'function' ? next() : next;
        },
    };
    ctx.window = ctx;
    if (options.navigateTo) ctx.navigateTo = (...args) => navigations.push(args);

    vm.runInNewContext(source, ctx, { filename: 'todos.js' });
    return {
        ctx, node, findById,
        header: selectors.get('.workbench-header') || null,
        sidebarBadge: selectors.get('.sidebar-item[data-view="todo"] .nav-badge') || null,
        listRequests: () => requests.filter(r => r.url.startsWith('/api/todos?')),
        summaryRequests: () => requests.filter(r => r.url === '/api/todos/summary'),
        hidden: id => node(id).classList.contains('hidden'),
        fireFocus: () => focusListeners.forEach(fn => fn()),
        navigations,
    };
}

function assertFailedView(h, message = messages.todo_load_failed) {
    assert.equal(h.hidden('todo-banner'), false);
    assert.equal(h.node('todo-banner').textContent, message);
    assert.equal(h.hidden('todo-empty'), true, 'failure must not claim the list is empty');
    assert.equal(h.hidden('todo-list'), true);
    assert.equal(h.hidden('todo-pagination'), true);
}

for (const [name, failedResponse] of [
    ['storage unavailable', () => response({ status: 'error', code: 'unavailable', message: '/private/server/path' }, 503)],
    ['server error', () => response({ status: 'error', code: 'internal', message: '/private/server/path' }, 500)],
    ['legacy HTTP 200 error body', () => response({ status: 'error', code: 'unavailable', message: '/private/server/path' })],
    ['non-JSON response', () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('invalid JSON'); } })],
    ['network failure', () => { throw new TypeError('Failed to fetch'); }],
]) {
    test(`${name} shows only a localized failure and retries on revisit`, async () => {
        const h = setup([failedResponse, response({ status: 'success', items: [], total: 0 })]);
        await h.ctx.loadTodosView();
        assertFailedView(h);
        await h.ctx.loadTodosView();
        assert.equal(h.listRequests().length, 2, 'revisiting retries without a forced refresh');
        assert.equal(h.hidden('todo-banner'), true);
        assert.equal(h.hidden('todo-empty'), false);
        assert.equal(h.node('todo-empty-text').textContent, messages.todo_empty_open);
        await h.ctx.loadTodosView();
        assert.equal(h.listRequests().length, 2, 'successful loads remain cached');
    });
}

for (const [code, status, expected] of [
    ['unauthorized', 401, messages.todo_unauthorized_banner],
    ['todo_disabled', 404, messages.todo_disabled_banner],
]) {
    test(`${code} retains its localized message without an empty result`, async () => {
        const h = setup([response({ status: 'error', code, message: 'server internals' }, status)]);
        await h.ctx.loadTodosView();
        assertFailedView(h, expected);
    });
}

test('failed refresh clears stale results and pagination, then retries on revisit', async () => {
    let finishRefresh;
    const h = setup([
        response({ status: 'success', items: [{ id: 'todo-1', title: 'Existing task', status: 'pending' }], total: 21 }),
        () => new Promise(resolve => { finishRefresh = resolve; }),
        response({ status: 'success', items: [], total: 0 }),
    ]);
    await h.ctx.loadTodosView();
    assert.equal(h.hidden('todo-list'), false);
    assert.match(h.node('todo-list').innerHTML, /Existing task/);
    assert.equal(h.hidden('todo-pagination'), false);

    const refresh = h.ctx.loadTodosView(true);
    assert.equal(h.hidden('todo-pagination'), true, 'stale pagination is hidden while loading');
    finishRefresh(response({ status: 'error', code: 'unavailable' }, 503));
    await refresh;
    assertFailedView(h);
    assert.equal(h.node('todo-list').innerHTML, '');
    assert.equal(h.node('todo-pagination').innerHTML, '');

    await h.ctx.loadTodosView();
    assert.equal(h.listRequests().length, 3);
    assert.equal(h.hidden('todo-empty'), false);
});

// ---------------------------------------------------------------------------
// Top-bar bell entry (change add-todo-header-bell)
// ---------------------------------------------------------------------------

test('bell mounts before the tenant selector and is idempotent', () => {
    const h = setup([], { mountBell: true, absent: BELL_IDS });
    const bell = h.findById('todo-bell-btn');
    assert.ok(bell, 'bell entry is mounted');
    assert.equal(h.header.children[0], bell, 'bell sits before the tenant selector');
    assert.equal(h.header.children[1].id, 'tenant-selector');
    assert.equal(bell.getAttribute('data-tip-key'), 'menu_todo');
    assert.equal(bell.getAttribute('data-tooltip'), messages.menu_todo);
    assert.equal(bell.getAttribute('aria-label'), messages.menu_todo);
    assert.equal(bell.getAttribute('data-tooltip-pos'), 'bottom');
    const badge = h.findById('todo-bell-badge');
    assert.ok(badge, 'bell carries its count badge');
    assert.equal(badge.parentNode, bell);

    h.ctx.mountTodoBell();
    assert.equal(
        h.header.children.filter(child => child.id === 'todo-bell-btn').length, 1,
        'a second mount does not add a second entry',
    );
});

test('bell count shows the capped summary total and hides at zero', async () => {
    for (const [open, expected] of [[3, '3'], [0, ''], [120, '99+']]) {
        const h = setup([], {
            mountBell: true,
            absent: BELL_IDS,
            summary: { status: 'success', enabled: true, bound: true, open, overdue: 0 },
        });
        await h.ctx.refreshSummary();
        const bell = h.findById('todo-bell-btn');
        const badge = h.findById('todo-bell-badge');
        assert.equal(bell.classList.contains('hidden'), false, `${open}: the entry stays`);
        if (expected) {
            assert.equal(badge.classList.contains('hidden'), false, `${open}: the count shows`);
            assert.equal(badge.textContent, expected, `${open}: the count is capped correctly`);
        } else {
            assert.equal(badge.classList.contains('hidden'), true, '0 hides the count, not the entry');
            assert.equal(badge.textContent, '');
        }
    }
});

test('bell entry hides on an authoritative denial, survives a transient failure', async () => {
    for (const [name, summary] of [
        ['capability withdrawn', { status: 'success', enabled: false, bound: false, open: 0, overdue: 0 }],
        ['no read permission', () => response({ status: 'error', code: 'unauthorized', message: 'nope' }, 403)],
        ['feature off', () => response({ status: 'error', code: 'todo_disabled', message: 'nope' }, 404)],
    ]) {
        const h = setup([], { mountBell: true, absent: BELL_IDS, summary });
        await h.ctx.refreshSummary();
        assert.equal(h.findById('todo-bell-btn').classList.contains('hidden'), true, `${name} hides the entry`);
        assert.equal(h.findById('todo-bell-badge').classList.contains('hidden'), true, `${name} shows no count`);
    }

    const failed = setup([], {
        mountBell: true,
        absent: BELL_IDS,
        summary: () => response({ status: 'error', code: 'unavailable', message: 'nope' }, 503),
    });
    await failed.ctx.refreshSummary();
    assert.equal(failed.findById('todo-bell-btn').classList.contains('hidden'), false,
        'a failed read is not a verdict: the entry survives');
    assert.equal(failed.findById('todo-bell-badge').classList.contains('hidden'), true,
        'but a stale count is not kept');
});

test('clicking the bell navigates to the todo view', () => {
    const h = setup([], { mountBell: true, absent: BELL_IDS, navigateTo: true });
    h.findById('todo-bell-btn').fire('click');
    assert.deepEqual(h.navigations, [['todo']]);

    const withoutNav = setup([], { mountBell: true, absent: BELL_IDS });
    withoutNav.findById('todo-bell-btn').fire('click'); // must not throw
});

test('window focus refreshes the summary without loading the list', async () => {
    const h = setup([], {
        mountBell: true,
        absent: BELL_IDS,
        summary: { status: 'success', enabled: true, bound: true, open: 7, overdue: 0 },
    });
    await h.ctx.refreshSummary();
    const before = h.summaryRequests().length;
    h.fireFocus();
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(h.summaryRequests().length, before + 1, 'focus re-reads the summary');
    assert.equal(h.listRequests().length, 0, 'focus must not pull the todo list');
    assert.equal(h.findById('todo-bell-badge').textContent, '7');
});

// ---------------------------------------------------------------------------
// Sole entry point (change remove-todo-sidebar-entry)
// ---------------------------------------------------------------------------

test('the sidebar template no longer offers a todo entry', () => {
    const html = fs.readFileSync(path.join(__dirname, '../channel/web/chat.html'), 'utf8');
    // Count rather than assert.doesNotMatch: the latter dumps the whole template
    // into the failure message.
    const entries = (html.match(/data-view="todo"/g) || []).length;
    assert.equal(entries, 0,
        'the sidebar must not carry a todo entry: the top-bar bell is the only way in');
});

test('the sidebar badge styling is gone with the entry it dressed', () => {
    const css = fs.readFileSync(path.join(__dirname, '../channel/web/static/css/appearance.css'), 'utf8');
    // The rules guarded the removed row's count pill; leaving them behind would
    // keep a stylesheet comment describing a sidebar badge nothing can render.
    const rules = (css.match(/\.nav-badge/g) || []).length;
    assert.equal(rules, 0,
        'the collapsed-rail and overdue rules for the sidebar badge go with the entry');
});

test('the summary drives the bell badge only, never a sidebar badge', async () => {
    const h = setup([], {
        mountBell: true,
        absent: BELL_IDS,
        sidebarTodo: true,
        summary: { status: 'success', enabled: true, bound: true, open: 3, overdue: 0 },
    });
    assert.ok(h.sidebarBadge, 'the stand-in sidebar badge exists for this assertion');
    await h.ctx.refreshSummary();
    assert.equal(h.sidebarBadge.textContent, '',
        'the removed sidebar badge is no longer a render target');
    assert.equal(h.sidebarBadge.classList.contains('hidden'), true,
        'nothing reveals the sidebar badge');
    assert.equal(h.findById('todo-bell-badge').textContent, '3',
        'the bell is the only place a count appears');
});
