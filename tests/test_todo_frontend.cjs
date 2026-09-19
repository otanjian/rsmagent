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
    todo_empty_delegated: '暂无委派出去的事项',
    todo_disabled_banner: '待办功能未开启，可在配置中启用 todo_enabled。',
    todo_unauthorized_banner: '请先登录后再使用待办功能。',
    menu_todo: '我的待办',
    todo_scope_mine: '我的待办',
    todo_scope_delegated: '我委派的',
    todo_action_recall: '收回',
    todo_action_reject: '退回',
    todo_action_assign: '指派',
    todo_action_transfer: '转交',
    todo_action_complete: '完成',
    todo_action_cancel: '取消',
    todo_action_edit: '编辑',
    todo_field_assignee: '接收人',
    todo_field_assignee_none: '留空（我自己跟进）',
    todo_delegate_closed: '当前账号未开放委派，接收人不可选。',
    todo_delegate_need_target: '请先选择一位接收人',
    todo_delegated: '已委派',
    todo_delegate_failed: '委派失败',
    todo_recalled: '已收回',
    todo_rejected: '已退回',
    todo_delegatee_unknown: '未知成员',
    todo_created: '已创建',
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
            toggle(name, force) {
                const on = force === undefined ? !classes.has(name) : !!force;
                if (on) classes.add(name); else classes.delete(name);
                return on;
            },
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
        focus() {},
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

// The badge's polling interval, per todo-workbench: no shorter than 30 seconds.
const POLL_MS = 30000;

// Yield to the microtask queue so an in-flight async refresh can settle.
const flush = () => new Promise(resolve => setImmediate(resolve));

// A hand-cranked clock: the module's cadence has to be observable without
// sleeping. It flushes pending work before and after each timer it runs, so a
// tick that fires an async read settles before the next one is looked for.
function fakeClock() {
    let now = 0;
    let seq = 0;
    let timers = [];
    return {
        setTimeout(fn, ms) {
            const handle = ++seq;
            timers.push({ handle, fn, due: now + (Number(ms) || 0) });
            return handle;
        },
        clearTimeout(handle) {
            timers = timers.filter(timer => timer.handle !== handle);
        },
        get pending() { return timers.length; },
        // The crank is also the module's wall clock (see the Date shim below),
        // so a throttled probe can be tested without waiting a real interval.
        get now() { return now; },
        async advance(ms) {
            now += ms;
            await flush();
            for (let guard = 0; guard < 20; guard++) {
                const at = timers.findIndex(timer => timer.due <= now);
                if (at < 0) return;
                const [timer] = timers.splice(at, 1);
                timer.fn();
                await flush();
            }
            throw new Error('timers kept rescheduling without making progress');
        },
    };
}

function setup(responses, options = {}) {
    const nodes = new Map();
    const creations = [];
    const absent = options.absent || new Set();
    const selectors = new Map();
    const focusListeners = [];
    const navigations = [];
    const clock = fakeClock();
    // The cadence is driven by the hand-cranked clock, but the module throttles
    // its revive probe on wall-clock time. Fold the crank into Date.now() so
    // both advance together; everything else about Date stays real (the module
    // also formats due dates with it).
    const RealDate = Date;
    function FakeDate(...args) {
        return args.length ? new RealDate(...args) : new RealDate();
    }
    FakeDate.now = () => RealDate.now() + clock.now;
    FakeDate.parse = RealDate.parse;
    FakeDate.UTC = RealDate.UTC;
    // Listener registries keyed by event type: the module now watches both
    // window focus and document visibility.
    const documentListeners = new Map();
    const windowListeners = new Map();
    const collect = (registry, type, fn) => {
        if (!registry.has(type)) registry.set(type, []);
        registry.get(type).push(fn);
    };
    const fire = (registry, type) => (registry.get(type) || []).forEach(fn => fn());
    // A start parked behind the login gate, released via releaseAuthGate().
    const gatedStarts = [];

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
        Date: FakeDate,
        document: {
            readyState: 'complete',
            visibilityState: 'visible',
            // The toast helper appends to the body; without one, every success
            // path would throw inside its own try/catch and be misreported.
            body: element('body'),
            getElementById: id => {
                const hit = findById(id);
                if (hit) return hit;
                if (absent.has(id)) return null;
                return node(id);
            },
            querySelector: sel => selectors.get(sel) || null,
            querySelectorAll: () => [],
            addEventListener(type, fn) { collect(documentListeners, type, fn); },
            createElement: tag => {
                const el = element(tag);
                creations.push(el);
                return el;
            },
        },
        addEventListener(type, fn) {
            collect(windowListeners, type, fn);
            if (type === 'focus') focusListeners.push(fn);
        },
        setTimeout: (fn, ms) => clock.setTimeout(fn, ms),
        clearTimeout: handle => clock.clearTimeout(handle),
        requestAuthGatedStart: fn => {
            if (options.authGate === 'closed') { gatedStarts.push(fn); return; }
            if (options.authGate === 'absent') return;   // no helper in this host
            fn();
        },
        fetch: async (url, opts) => {
            requests.push({ url, options: opts });
            if (url === '/api/todos/summary') return summaryReply();
            if (url === '/api/todos/assignees') {
                // The receiver directory: a rejection is how the server says
                // delegation is not open for this account.
                if (typeof options.assignees === 'function') return options.assignees();
                return response(options.assignees || { status: 'success', items: [] });
            }
            if (url === '/api/todos' && opts && opts.method === 'POST') {
                assert.ok(options.creates && options.creates.length, 'unexpected create request');
                const next = options.creates.shift();
                return typeof next === 'function' ? next() : next;
            }
            if (/\/delegation$/.test(url)) {
                assert.ok(options.delegations && options.delegations.length,
                    'unexpected delegation request');
                const next = options.delegations.shift();
                return typeof next === 'function' ? next() : next;
            }
            assert.match(url, /^\/api\/todos(\/delegated)?\?/);
            assert.ok(responses.length, 'unexpected list request');
            const next = responses.shift();
            return typeof next === 'function' ? next() : next;
        },
    };
    ctx.window = ctx;
    // The absent-gate host must not even see the helper.
    if (options.authGate === 'absent') delete ctx.requestAuthGatedStart;
    if (options.navigateTo) ctx.navigateTo = (...args) => navigations.push(args);

    vm.runInNewContext(source, ctx, { filename: 'todos.js' });
    return {
        ctx, node, findById,
        header: selectors.get('.workbench-header') || null,
        sidebarBadge: selectors.get('.sidebar-item[data-view="todo"] .nav-badge') || null,
        listRequests: () => requests.filter(r => r.url.startsWith('/api/todos?')),
        delegatedRequests: () => requests.filter(r => r.url.startsWith('/api/todos/delegated?')),
        delegationPosts: () => requests.filter(r => /\/delegation$/.test(r.url)),
        summaryRequests: () => requests.filter(r => r.url === '/api/todos/summary'),
        hidden: id => node(id).classList.contains('hidden'),
        fireFocus: () => focusListeners.forEach(fn => fn()),
        navigations,
        advance: ms => clock.advance(ms),
        pendingTimers: () => clock.pending,
        setVisibility: state => {
            ctx.document.visibilityState = state;
            fire(documentListeners, 'visibilitychange');
            fire(windowListeners, 'visibilitychange');
        },
        // Activity on the shell (a click or a keystroke) is the module's cheap
        // signal that a parked cadence may be worth reviving.
        fireDocument: type => fire(documentListeners, type),
        releaseAuthGate: () => gatedStarts.splice(0).forEach(fn => fn()),
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

// ---------------------------------------------------------------------------
// Bell badge polling (change add-todo-bell-badge-polling)
// ---------------------------------------------------------------------------

const OPEN_SUMMARY = { status: 'success', enabled: true, bound: true, open: 3, overdue: 0 };

test('the bell count refreshes one interval at a time without pulling the list', async () => {
    const h = setup([], { mountBell: true, absent: BELL_IDS, summary: OPEN_SUMMARY });
    await flush();                                   // the page-load read lands
    const initial = h.summaryRequests().length;
    await h.advance(POLL_MS - 1);
    assert.equal(h.summaryRequests().length, initial, 'nothing fires before the interval elapses');
    await h.advance(1);
    assert.equal(h.summaryRequests().length, initial + 1, 'one poll fires at the interval');
    assert.equal(h.listRequests().length, 0, 'polling must never pull the todo list');
    assert.equal(h.findById('todo-bell-badge').textContent, '3', 'the poll repaints the count');
});

test('polling pauses while the tab is hidden and refreshes when it returns', async () => {
    const h = setup([], { mountBell: true, absent: BELL_IDS, summary: OPEN_SUMMARY });
    await flush();
    const initial = h.summaryRequests().length;

    h.setVisibility('hidden');
    await h.advance(POLL_MS);
    await h.advance(POLL_MS);
    assert.equal(h.summaryRequests().length, initial, 'a hidden tab sends nothing');
    assert.equal(h.pendingTimers(), 0, 'and leaves no tick armed behind it');

    h.setVisibility('visible');
    await flush();
    assert.equal(h.summaryRequests().length, initial + 1, 'becoming visible re-reads immediately');
    await h.advance(POLL_MS - 1);
    assert.equal(h.summaryRequests().length, initial + 1, 'the interval restarts from the resume');
    await h.advance(1);
    assert.equal(h.summaryRequests().length, initial + 2, 'and the cadence continues');
});

test('polling stays parked until the login gate opens', async () => {
    const h = setup([], {
        mountBell: true, absent: BELL_IDS, authGate: 'closed', summary: OPEN_SUMMARY,
    });
    const parked = h.summaryRequests().length;
    await h.advance(POLL_MS);
    await h.advance(POLL_MS);
    assert.equal(h.summaryRequests().length, parked, 'the cadence waits behind the gate');
    h.releaseAuthGate();
    await h.advance(POLL_MS);
    assert.equal(h.summaryRequests().length, parked + 1, 'the cadence runs once the gate opens');

    const gated = setup([], {
        mountBell: true, absent: BELL_IDS, authGate: 'absent', summary: OPEN_SUMMARY,
    });
    await flush();                                    // the page-load read lands first
    const before = gated.summaryRequests().length;
    await gated.advance(POLL_MS);
    assert.equal(gated.summaryRequests().length, before + 1,
        'a host without the gate helper still polls');
});

test('an explicit refresh restarts the interval instead of stacking a poll', async () => {
    const h = setup([], { mountBell: true, absent: BELL_IDS, summary: OPEN_SUMMARY });
    await flush();
    const initial = h.summaryRequests().length;

    await h.advance(POLL_MS / 2);
    h.fireFocus();
    await flush();
    assert.equal(h.summaryRequests().length, initial + 1, 'focus still refreshes the count');

    await h.advance(POLL_MS - 1);
    assert.equal(h.summaryRequests().length, initial + 1, 'the interval restarts from the refresh');
    await h.advance(1);
    assert.equal(h.summaryRequests().length, initial + 2, 'the next poll lands one full interval later');
});

test('a slow summary read never stacks a second request', async () => {
    const resolvers = [];
    const h = setup([], {
        mountBell: true, absent: BELL_IDS,
        summary: () => new Promise(resolve => resolvers.push(resolve)),
    });
    const inFlight = h.summaryRequests().length;      // the page-load read, still pending
    await h.advance(POLL_MS);
    assert.equal(h.summaryRequests().length, inFlight, 'the tick skips while a read is in flight');

    resolvers[0](response({ status: 'success', enabled: true, bound: true, open: 5, overdue: 0 }));
    await flush();
    assert.equal(h.findById('todo-bell-badge').textContent, '5');
    await h.advance(POLL_MS);
    assert.equal(h.summaryRequests().length, inFlight + 1,
        'the next tick reads once the slow read has settled');
});

test('a superseded summary response never paints over a newer one', async () => {
    const resolvers = [];
    const h = setup([], {
        mountBell: true, absent: BELL_IDS,
        summary: () => new Promise(resolve => resolvers.push(resolve)),
    });
    h.fireFocus();                                    // supersedes the page-load read
    assert.equal(resolvers.length, 2, 'the later refresh issues its own read');

    resolvers[1](response({ status: 'success', enabled: true, bound: true, open: 9, overdue: 0 }));
    await flush();
    assert.equal(h.findById('todo-bell-badge').textContent, '9');

    resolvers[0](response({ status: 'success', enabled: true, bound: true, open: 1, overdue: 0 }));
    await flush();
    assert.equal(h.findById('todo-bell-badge').textContent, '9',
        'the late response from the previous context is dropped');
});

test('a lost session stops the cadence instead of polling with no cookie', async () => {
    let lost = false;
    const h = setup([], {
        mountBell: true, absent: BELL_IDS,
        summary: () => (lost
            ? response({ status: 'error', code: 'unauthorized', message: 'nope' }, 401)
            : response(OPEN_SUMMARY)),
    });
    await flush();                                    // the page-load read succeeds
    await h.advance(POLL_MS);
    assert.equal(h.summaryRequests().length, 2, 'the cadence runs while the session is alive');

    lost = true;
    await h.advance(POLL_MS);
    assert.equal(h.summaryRequests().length, 3, 'the poll that meets the 401 still reads once');
    assert.equal(h.findById('todo-bell-btn').classList.contains('hidden'), true,
        'a lost session is an authoritative denial');

    await h.advance(POLL_MS);
    await h.advance(POLL_MS);
    assert.equal(h.summaryRequests().length, 3, 'and then nothing: no reads without a cookie');
});

test('a restored session resumes the cadence at the next refresh point', async () => {
    let lost = true;
    const h = setup([], {
        mountBell: true, absent: BELL_IDS,
        summary: () => (lost
            ? response({ status: 'error', code: 'unauthorized', message: 'nope' }, 401)
            : response(OPEN_SUMMARY)),
    });
    await flush();                                    // the page-load read 401s
    const parked = h.summaryRequests().length;

    lost = false;                                     // the user signs back in
    h.setVisibility('hidden');
    h.setVisibility('visible');                       // the shell is looked at again
    await flush();
    assert.equal(h.summaryRequests().length, parked + 1,
        'becoming visible re-reads even while the cadence is parked');

    await h.advance(POLL_MS);
    assert.equal(h.summaryRequests().length, parked + 2,
        'a good read restarts the cadence');
});

test('a tab with no tenant context parks the cadence instead of reading every interval', async () => {
    // The login overlay answers summary with 400 missing_tenant rather than 401,
    // so the park has to cover that refusal too: reading it every interval would
    // be exactly the cookie-less polling the login gate exists to prevent.
    const h = setup([], {
        mountBell: true, absent: BELL_IDS,
        summary: () => response({
            status: 'error', code: 'missing_tenant', message: 'tenant selection required',
        }, 400),
    });
    await flush();                                    // the page-load read is refused
    const parked = h.summaryRequests().length;

    await h.advance(POLL_MS);
    await h.advance(POLL_MS);
    assert.equal(h.summaryRequests().length, parked,
        'no reads while the shell has no tenant context');
});

test('user activity revives a parked cadence, at most once per interval', async () => {
    let signedOut = true;
    const h = setup([], {
        mountBell: true, absent: BELL_IDS,
        summary: () => (signedOut
            ? response({ status: 'error', code: 'missing_tenant', message: 'no tenant' }, 400)
            : response(OPEN_SUMMARY)),
    });
    await flush();
    const parked = h.summaryRequests().length;

    h.fireDocument('keydown');                        // the user types at the login form
    h.fireDocument('keydown');
    h.fireDocument('pointerdown');
    await flush();
    assert.equal(h.summaryRequests().length, parked + 1,
        'one typed burst probes at most once, so a password cannot spray requests');

    signedOut = false;                                // the session comes back
    await h.advance(POLL_MS);
    h.fireDocument('keydown');                        // the next activity probes again
    await flush();
    assert.equal(h.summaryRequests().length, parked + 2, 'activity probes once the interval passed');
    assert.equal(h.findById('todo-bell-badge').textContent, '3',
        'the probe that succeeds paints the count again');

    await h.advance(POLL_MS);
    assert.equal(h.summaryRequests().length, parked + 3,
        'a successful probe restarts the fixed-interval cadence');
});

test('a visibilitychange that is not a resume does not read', async () => {
    // Only a hidden -> visible transition is the resume the spec asks for; an
    // event that arrives while the tab is already visible must not spend a read.
    const h = setup([], { mountBell: true, absent: BELL_IDS, summary: OPEN_SUMMARY });
    await flush();
    const initial = h.summaryRequests().length;

    h.setVisibility('visible');
    await flush();
    assert.equal(h.summaryRequests().length, initial,
        'no read without a hidden -> visible transition');

    await h.advance(POLL_MS);
    assert.equal(h.summaryRequests().length, initial + 1, 'the interval is untouched');
});

// ---------------------------------------------------------------------------
// Delegation (change add-todo-delegation)
// ---------------------------------------------------------------------------

const HANDED_OUT = {
    id: 'todo-hand', title: 'Send the report', status: 'pending',
    status_label: '待处理', kind: 'general', kind_label: '普通事项',
    priority: 'normal', priority_label: '普通', version: 3,
    owner_id: 'user-me', assignee_id: 'user-bob',
    assignee: { id: 'user-bob', username: 'bob', display_name: 'Bob' },
    mine: false, delegated_by_me: true, can_edit: false,
    can_operate: { start: false, complete: false, cancel: false, reopen: false },
    can_assign: false, can_transfer: false, can_reject: false, can_recall: true,
};

const MY_OWN = {
    id: 'todo-mine', title: 'My own task', status: 'pending',
    status_label: '待处理', kind: 'general', kind_label: '普通事项',
    priority: 'normal', priority_label: '普通', version: 1,
    owner_id: 'user-me', assignee_id: 'user-me',
    assignee: { id: 'user-me', username: 'me', display_name: '' },
    mine: true, delegated_by_me: false, can_edit: true,
    can_operate: { start: true, complete: true, cancel: true, reopen: false },
    can_assign: true, can_transfer: false, can_reject: false, can_recall: false,
};

test('the delegated section reads its own feed and leaves the badge alone', async () => {
    const h = setup([
        response({ status: 'success', items: [MY_OWN], total: 1 }),
        response({ status: 'success', items: [HANDED_OUT], total: 1 }),
        response({ status: 'success', items: [MY_OWN], total: 1 }),
    ], { mountBell: true, absent: BELL_IDS, summary: OPEN_SUMMARY });
    await h.ctx.loadTodosView();
    const summaries = h.summaryRequests().length;

    await h.ctx.setTodoScope('delegated');
    assert.equal(h.delegatedRequests().length, 1, 'the section has its own endpoint');
    assert.equal(h.listRequests().length, 1, 'and does not re-read the personal list');
    assert.equal(h.summaryRequests().length, summaries,
        'handed-out work never triggers a badge recount of its own');
    assert.match(h.node('todo-list').innerHTML, /Send the report/);

    await h.ctx.setTodoScope('mine');
    assert.equal(h.delegatedRequests().length, 1, 'coming back does not touch the other feed');
    assert.equal(h.listRequests().length, 2);
    assert.match(h.node('todo-list').innerHTML, /My own task/);
    assert.doesNotMatch(h.node('todo-list').innerHTML, /Send the report/,
        'the two sections are never mixed into one list');
});

test('a handed-out row names its handler and offers recall only', async () => {
    const h = setup([
        response({ status: 'success', items: [HANDED_OUT], total: 1 }),
    ]);
    await h.ctx.setTodoScope('delegated');

    const html = h.node('todo-list').innerHTML;
    assert.match(html, /Bob/, 'the current handler is shown');
    assert.match(html, /recallTodo\('todo-hand',3\)/, 'recall is offered inline');
    assert.doesNotMatch(html, /operateTodo/, 'no complete / cancel for somebody else\'s work');
    assert.doesNotMatch(html, /openTodoEdit/, 'and no edit entry');
});

test('the empty delegated section says so instead of looking broken', async () => {
    const h = setup([response({ status: 'success', items: [], total: 0 })]);
    await h.ctx.setTodoScope('delegated');
    assert.equal(h.hidden('todo-empty'), false);
    assert.equal(h.node('todo-empty-text').textContent, messages.todo_empty_delegated);
});

test('the receiver picker is offered when the server allows delegation', async () => {
    const h = setup([response({ status: 'success', items: [], total: 0 })], {
        assignees: { status: 'success', items: [{ username: 'bob', display_name: 'Bob' }] },
    });
    await h.ctx.loadTodosView();
    await flush();
    h.ctx.openTodoCreate();
    assert.equal(h.hidden('todo-edit-delegatee-field'), false);
    assert.equal(h.hidden('todo-edit-delegatee-picker'), false);
    assert.equal(h.hidden('todo-edit-delegatee-closed'), true);
    const options = h.node('todo-edit-field-assignee').innerHTML;
    assert.match(options, /value=""[^>]*>留空（我自己跟进）/, 'the empty choice is the default');
    assert.match(options, /value="bob"[^>]*>Bob/, 'and the colleague is selectable by login name');
});

test('without todo.assign the picker is withheld and explained', async () => {
    const h = setup([response({ status: 'success', items: [], total: 0 })], {
        assignees: () => response({ status: 'error', code: 'forbidden', message: 'no' }, 403),
    });
    await h.ctx.loadTodosView();
    await flush();
    h.ctx.openTodoCreate();
    assert.equal(h.hidden('todo-edit-delegatee-field'), false, 'the reason is shown where the control was');
    assert.equal(h.hidden('todo-edit-delegatee-picker'), true, 'no selector is offered');
    assert.equal(h.hidden('todo-edit-delegatee-closed'), false);
    assert.equal(h.node('todo-edit-field-assignee').innerHTML, '', 'nothing to choose from');
});

test('the picker is not offered on an existing item', async () => {
    const h = setup([response({ status: 'success', items: [MY_OWN], total: 1 })], {
        assignees: { status: 'success', items: [{ username: 'bob', display_name: 'Bob' }] },
    });
    await h.ctx.loadTodosView();
    await flush();
    h.ctx.openTodoEdit('todo-mine');
    assert.equal(h.hidden('todo-edit-delegatee-field'), true,
        'an existing item changes hands through the drawer actions, not this form');
});

test('choosing a receiver creates the item and then hands it over', async () => {
    const h = setup([
        response({ status: 'success', items: [], total: 0 }),
        response({ status: 'success', items: [], total: 0 }),
    ], {
        assignees: { status: 'success', items: [{ username: 'bob', display_name: 'Bob' }] },
        creates: [response({ status: 'success', item: { id: 'todo-9', version: 1 } })],
        delegations: [response({
            status: 'success',
            item: { id: 'todo-9', version: 2, assignee_id: 'user-bob' },
        })],
    });
    await h.ctx.loadTodosView();
    await flush();
    h.ctx.openTodoCreate();
    h.node('todo-edit-field-title').value = 'Hand this over';
    h.node('todo-edit-field-assignee').value = 'bob';
    await h.ctx.submitTodoEdit();

    const post = h.delegationPosts();
    assert.equal(post.length, 1, 'the handover is a second, explicit step');
    assert.equal(post[0].url, '/api/todos/todo-9/delegation');
    const body = JSON.parse(post[0].options.body);
    assert.deepEqual(body, { action: 'assign', assignee: 'bob', expected_version: 1 });
});

test('with no receiver chosen nothing is handed over', async () => {
    const h = setup([
        response({ status: 'success', items: [], total: 0 }),
        response({ status: 'success', items: [], total: 0 }),
    ], {
        assignees: { status: 'success', items: [{ username: 'bob', display_name: 'Bob' }] },
        creates: [response({ status: 'success', item: { id: 'todo-9', version: 1 } })],
    });
    await h.ctx.loadTodosView();
    await flush();
    h.ctx.openTodoCreate();
    h.node('todo-edit-field-title').value = 'Keep it';
    h.node('todo-edit-field-assignee').value = '';
    await h.ctx.submitTodoEdit();
    assert.equal(h.delegationPosts().length, 0, 'no receiver, no delegation call');
});

test('a failed handover is reported instead of claiming success', async () => {
    const h = setup([
        response({ status: 'success', items: [], total: 0 }),
        response({ status: 'success', items: [], total: 0 }),
    ], {
        assignees: { status: 'success', items: [{ username: 'bob', display_name: 'Bob' }] },
        creates: [response({ status: 'success', item: { id: 'todo-9', version: 1 } })],
        delegations: [response({ status: 'error', code: 'forbidden', message: 'nope' }, 403)],
    });
    h.ctx.confirm = () => true;
    await h.ctx.loadTodosView();
    await flush();
    h.ctx.openTodoCreate();
    h.node('todo-edit-field-title').value = 'Stays with me';
    h.node('todo-edit-field-assignee').value = 'bob';
    await h.ctx.submitTodoEdit();

    assert.equal(h.delegationPosts().length, 1);
    // The create succeeded, so the modal closes and the list is reloaded: the
    // item exists and stays with the user. Only the handover is reported as
    // failed (the toast), never as a delegation that landed.
    assert.equal(h.hidden('todo-edit-overlay'), true, 'the created item is not rolled back');
    assert.equal(h.listRequests().length, 2, 'the list reflects the item that does exist');
});

test('recall posts the verb with the version the row carried', async () => {
    const h = setup([
        response({ status: 'success', items: [HANDED_OUT], total: 1 }),
        response({ status: 'success', items: [MY_OWN], total: 1 }),
    ], {
        delegations: [response({
            status: 'success',
            item: Object.assign({}, HANDED_OUT, { mine: true, can_recall: false, can_edit: true }),
        })],
    });
    await h.ctx.setTodoScope('delegated');
    h.ctx.confirm = () => true;
    await h.ctx.recallTodo('todo-hand', 3);

    const body = JSON.parse(h.delegationPosts()[0].options.body);
    assert.deepEqual(body, { action: 'recall', expected_version: 3 });
    assert.equal(h.delegatedRequests().length, 2, 'the section reloads after the recall');
});

test('a declined recall sends nothing', async () => {
    const h = setup([response({ status: 'success', items: [HANDED_OUT], total: 1 })]);
    await h.ctx.setTodoScope('delegated');
    h.ctx.confirm = () => false;
    await h.ctx.recallTodo('todo-hand', 3);
    assert.equal(h.delegationPosts().length, 0, 'the user said no, so no request is made');
});
