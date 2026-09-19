// Console-side acceptance for the four recovered pages (tasks 9.4/9.5).
//
// tests/test_capability_matrix.py and tests/test_recovered_entry_acceptance.py
// lock the registry and the gate; tests/test_recovered_pages_console.py locks
// the payload the console reads. This file locks what the *console* does with
// that payload:
//
//   * the recovered views take their availability from the server projection
//     (VIEW_META page key + console_pages), so the client cannot disagree with
//     the server about whether the page exists;
//   * a refusal is rendered from the server's own reason payload, never as a
//     successful empty state;
//   * a refused page is not preloaded: the projection gate runs before the lazy
//     loader, and the loaders are only reached from navigation dispatch;
//   * the per-task verbs the scheduler cards offer are the server's per-task
//     decision (`task.capabilities`), not a client-side guess.
//
// The neighbouring files already lock: the closed-consumer / generic-error /
// success paths of loadTasksView (test_scheduler_frontend.cjs), the
// channelsFailureKey / scanFailureText / renderChannelsUnavailable vocabulary
// (test_tenant_channel_frontend.cjs, test_tenant_channel_card_frontend.cjs) and
// _viewNavDenied for `channels` and `history`
// (test_channel_scope_nav_frontend.cjs, test_workbench_menu_grant_frontend.cjs).
// This file covers the four recovered pages specifically and does not repeat
// them.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const read = p => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const consoleJs = read('channel/web/static/js/console.js');
const chatHtml = read('channel/web/chat.html');

// The view ids the recovered pages are reached through, and the page key the
// server signs for each. The page keys are asserted *through the product code*
// below, so this list only names the views.
const RECOVERED_VIEWS = { tasks: 'workbench.schedules', memory: 'admin.memory', channels: 'admin.channels' };

// Extract one top-level function by brace matching (this file is ~18k lines and
// neighbouring sections would drag in unrelated dependencies). An `async `
// prefix is kept: without it the extracted source is a syntax error.
function fnSource(name) {
    const head = `function ${name}(`;
    const from = consoleJs.indexOf(head);
    assert.ok(from >= 0, `Missing ${name} in console.js`);
    const async = consoleJs.slice(Math.max(0, from - 6), from) === 'async ';
    let depth = 0;
    for (let i = consoleJs.indexOf('{', from); i < consoleJs.length; i++) {
        if (consoleJs[i] === '{') depth++;
        else if (consoleJs[i] === '}') {
            depth--;
            if (depth === 0) return consoleJs.slice(async ? from - 6 : from, i + 1);
        }
    }
    throw new Error(`Unbalanced ${name}`);
}

// The console loaders are fire-and-forget (they do not return their fetch
// promise), so a test has to let the microtask queue drain before asserting.
const flush = () => new Promise(resolve => setImmediate(resolve));

// Extract a top-level `const NAME = { ... };` object literal (VIEW_META holds
// the view -> console page mapping and is a plain object of strings).
function objectLiteral(name) {
    const from = consoleJs.indexOf(`const ${name} = {`);
    assert.ok(from >= 0, `Missing ${name} in console.js`);
    let depth = 0;
    for (let i = consoleJs.indexOf('{', from); i < consoleJs.length; i++) {
        if (consoleJs[i] === '{') depth++;
        else if (consoleJs[i] === '}') {
            depth--;
            if (depth === 0) return consoleJs.slice(from, i + 1);
        }
    }
    throw new Error(`Unbalanced ${name}`);
}

function element() {
    const classes = new Set(['hidden']);
    const el = {
        innerHTML: '', textContent: '', dataset: {}, children: [], style: {},
        classList: {
            add: (...x) => x.forEach(c => classes.add(c)),
            remove: (...x) => x.forEach(c => classes.delete(c)),
            contains: c => classes.has(c),
            toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
        },
        setAttribute() {}, removeAttribute() {}, addEventListener() {},
        focus() {}, closest() { return null; },
        querySelector(sel) { return (el._sel || {})[sel] || null; },
        appendChild(child) { el.children.push(child); return child; },
    };
    return el;
}

// =====================================================================
// 1. Availability comes from the server projection
// =====================================================================

function bootNav(context) {
    const sandbox = {
        _identityMode: () => 'database',
        _baseAuthContext: () => context,
        // console.js is a browser script: the Tasks mount looks up its feature
        // module through `window`. Absent the module the mount is a no-op, so an
        // empty window is the honest sandbox for these slices.
        window: {},
    };
    vm.createContext(sandbox);
    vm.runInContext(
        [objectLiteral('VIEW_META') + ';', fnSource('_consolePageForView'),
         fnSource('_viewNavDenied')].join('\n'), sandbox);
    return sandbox;
}

test('each recovered view resolves its page key from VIEW_META, not a client list', () => {
    const sandbox = bootNav(null);
    for (const [viewId, pageKey] of Object.entries(RECOVERED_VIEWS)) {
        assert.equal(sandbox._consolePageForView(viewId), pageKey,
            `${viewId} must resolve to the server's page key`);
        assert.equal(sandbox._consolePageForView('view-' + viewId), '',
            'an unknown id must not be treated as a signed page');
    }
    // The router names 'project browse' and the QR entry through no page at all
    // (the registry declares page=None), which is why the folder picker and the
    // scan panel read the response instead of a page key.
    assert.equal(sandbox._consolePageForView('projects'), '');
});

test('the availability gate reads the projection fields the server signs', () => {
    const gate = fnSource('_viewNavDenied');
    assert.match(gate, /console_pages/, 'the gate must read the server projection');
    assert.match(gate, /menu_denied/, 'the gate must honour the withheld menu grant');
    assert.match(gate, /available/, 'the gate must read the availability flag');
    assert.match(gate, /read_allowed/, 'the gate must read the read grant');
    // No page id is hard-wired to an availability verdict on the client.
    assert.doesNotMatch(consoleJs,
        /'(workbench\.schedules|admin\.memory|admin\.channels)'\s*:\s*(true|false)\b/,
        'the client must not carry its own page -> availability table');
});

test('the recovered views are not in the hardcoded not-shipped set', () => {
    const declared = /const UNAVAILABLE_VIEWS = new Set\(\[([^\]]*)\]\);/.exec(consoleJs);
    assert.ok(declared, 'UNAVAILABLE_VIEWS must stay declared');
    const sandbox = {};
    vm.createContext(sandbox);
    vm.runInContext('const set = new Set([' + declared[1] + ']);', sandbox);
    const ids = [...vm.runInContext('set', sandbox)];
    assert.deepStrictEqual(ids, ['backup', 'open_api']);
    for (const viewId of Object.keys(RECOVERED_VIEWS)) {
        assert.ok(!ids.includes(viewId),
            `${viewId} must take its state from the projection, not from a hardcoded set`);
    }
});

test('a withheld menu grant denies the recovered workbench page from the projection', () => {
    // The exact shape tests/test_recovered_pages_console.py asserts comes off
    // GET /auth/context for a role whose menu grant set omits the page.
    const denied = bootNav({
        authorization_mode: 'role',
        console_pages: {
            'workbench.schedules': { available: false, read_allowed: false, scope: 'self',
                reason: 'menu_not_granted', actions: {}, menu_denied: true },
        },
    });
    const verdict = denied._viewNavDenied('tasks');
    assert.ok(verdict && verdict.reason === 'denied', JSON.stringify(verdict));
    // The same page, granted: the gate must not invent a denial.
    const granted = bootNav({
        authorization_mode: 'role',
        console_pages: {
            'workbench.schedules': { available: true, read_allowed: true, scope: 'self',
                reason: '', actions: {} },
        },
    });
    assert.equal(granted._viewNavDenied('tasks'), null);
    // Unknown projection: never guess, never block (the server still enforces).
    assert.equal(bootNav(null)._viewNavDenied('tasks'), null);
});

test('a closed memory page is refused before the view loads', () => {
    const closed = bootNav({
        authorization_mode: 'role',
        console_pages: {
            'admin.memory': { available: false, read_allowed: false, scope: 'agent',
                reason: 'not_implemented', actions: {} },
        },
    });
    const verdict = closed._viewNavDenied('memory');
    assert.ok(verdict && verdict.reason === 'denied', JSON.stringify(verdict));
    const open = bootNav({
        authorization_mode: 'role',
        console_pages: {
            'admin.memory': { available: true, read_allowed: true, scope: 'agent',
                reason: '', actions: {} },
        },
    });
    assert.equal(open._viewNavDenied('memory'), null);
});

test('a refusal is rendered from the server reason payload, never as a shipped feature', () => {
    const show = fnSource('showUnavailableView');
    // Two distinct verdicts share one surface, and the copy is chosen from the
    // reason the caller passed in (which comes from _viewNavDenied).
    assert.match(show, /reason === 'denied'/, 'the denial branch must exist');
    assert.match(show, /nav_denied/, 'the denial copy must be the denied copy');
    assert.match(show, /nav_unavailable/, 'the not-open copy must stay for the rest');
    // The gate hands its own reason to the surface (`deny.reason`), so the
    // dispatch cannot silently substitute a literal string.
    const navigation = consoleJs.slice(consoleJs.indexOf('function navigateTo(viewId)'));
    assert.match(navigation, /const deny = _viewNavDenied\(viewId\);/);
    assert.match(navigation, /showUnavailableView\(viewId, deny\.reason\);/);
});

// =====================================================================
// 2. A refused page is not preloaded
// =====================================================================

test('navigation gates the view before its lazy loader runs', () => {
    const gate = consoleJs.indexOf('const deny = _viewNavDenied(viewId);');
    const origin = consoleJs.indexOf('_origNavigateTo(viewId);');
    const dispatch = consoleJs.indexOf("else if (viewId === 'tasks') loadTasksView();", origin);
    const memoryLoad = consoleJs.indexOf("switchMemoryTab('files');", origin);
    const channelsLoad = consoleJs.indexOf("else if (viewId === 'channels') loadChannelsView();", origin);
    assert.ok(gate > 0 && origin > 0 && dispatch > 0 && memoryLoad > 0 && channelsLoad > 0,
        'the dispatch sites must stay in console.js');
    // ordering: gate -> (refusal returns) -> base navigation -> lazy loaders
    assert.ok(gate < origin, 'the projection gate must run before navigating');
    assert.ok(origin < dispatch, 'the scheduler list loads only after dispatch');
    assert.ok(origin < memoryLoad, 'the memory list loads only after dispatch');
    assert.ok(origin < channelsLoad, 'the channels list loads only after dispatch');
    assert.match(consoleJs.slice(gate, origin),
        /if \(deny\) \{\s*showUnavailableView\(viewId, deny\.reason\);\s*return;/,
        'a denied page must return before any loader is reached');
});

test('the recovered views are not fetched by the startup path', () => {
    const init = fnSource('initApp');
    assert.doesNotMatch(init, /loadTasksView|loadMemoryView|loadChannelsView|_fpBrowse|switchMemoryTab/,
        'startup must not preload the recovered pages');
    // The scheduler list is guarded once per session and only filled on demand.
    assert.match(fnSource('loadTasksView'), /if \(tasksLoaded\) return;/);
});

// =====================================================================
// 3. The scheduler cards follow the server's per-task decision
// =====================================================================

// The Tasks section: `let tasksLoaded` plus the loader and its helpers. The
// list's DOM is stubbed; escapeHtml/agent lookups are enough for the assertions.
function bootTasks(payload) {
    const nodes = new Map();
    const ctx = {
        agentCatalog: [{ id: 'owner', name: 'Owner' }], currentLang: 'zh',
        t: key => key, // identity: assert on the raw key, locale-agnostic
        escapeHtml: x => String(x),
        findAgent: () => null,
        multiAgentMode: () => false,
        agentAvatarHTML: () => '',
        openTaskEditModal: () => {},
        showConfirmDialog: () => {},
        document: {
            getElementById(id) {
                if (!nodes.has(id)) {
                    const el = element();
                    if (id === 'tasks-empty') el.querySelector = sel => (sel === 'p' ? (el._p ||= element()) : null);
                    nodes.set(id, el);
                }
                return nodes.get(id);
            },
            createElement() {
                const el = element();
                el.querySelector = () => (el._q ||= element());
                return el;
            },
        },
        fetch: async () => ({ json: async () => payload }),
        // console.js is a browser script: the Tasks mount looks up its feature
        // module through `window`. Absent the module the mount is a no-op, so an
        // empty window is the honest sandbox for this slice.
        window: {},
    };
    vm.createContext(ctx);
    const from = consoleJs.indexOf('let tasksLoaded = false;');
    const to = consoleJs.indexOf('// =====================================================================\n// Logs View');
    assert.ok(from >= 0 && to > from, 'the scheduler section must stay in console.js');
    vm.runInContext(consoleJs.slice(from, to), ctx);
    return { ctx, get: id => ctx.document.getElementById(id) };
}

test('the scheduler card offers only the verbs the server granted that task', async () => {
    const { ctx, get } = bootTasks({
        status: 'success',
        tasks: [
            // An Agent-owned task the caller may see but not manage or run.
            { id: 'public-1', name: 'public-1', enabled: true, scope: 'public',
              agent_id: 'owner', schedule: { type: 'cron', expression: '0 2 * * *' },
              action: { content: 'shared work' },
              capabilities: { view: true, manage: false, run: false } },
            // The caller's own task: the same page offers the full verb set.
            { id: 'mine-1', name: 'mine-1', enabled: true, scope: 'personal',
              agent_id: 'owner', schedule: { type: 'cron', expression: '0 3 * * *' },
              action: { content: 'my work' },
              capabilities: { view: true, manage: true, run: true } },
        ],
    });
    await ctx.loadTasksView();
    const cards = get('tasks-list').children;
    assert.equal(cards.length, 2, 'both tasks are listed');

    const readOnly = cards[0].innerHTML;
    assert.match(readOnly, /public-1/, 'the read-only task is still visible');
    assert.doesNotMatch(readOnly, /task-run-now/, 'no run verb was granted');
    assert.doesNotMatch(readOnly, /type="checkbox"/, 'no manage verb was granted');

    const owned = cards[1].innerHTML;
    assert.match(owned, /task-run-now/, 'the granted run verb is offered');
    assert.match(owned, /type="checkbox"/, 'the granted manage verb is offered');
});

test('a closed scheduler answer is not rendered as a successful empty list', async () => {
    const closed = bootTasks({ status: 'error', code: 'database_unavailable' });
    await closed.ctx.loadTasksView();
    const closedText = closed.get('tasks-empty').querySelector('p').textContent;
    assert.equal(closedText, 'tasks_unavailable');
    assert.equal(closed.get('tasks-list').classList.contains('hidden'), true);
    assert.equal(closed.get('tasks-empty').classList.contains('hidden'), false);

    // The success-but-empty state is a *different* answer: the failure branch
    // must never reach it, or a closed consumer would look like "no tasks yet".
    const empty = bootTasks({ status: 'success', tasks: [] });
    await empty.ctx.loadTasksView();
    const emptyText = empty.get('tasks-empty').querySelector('p').textContent;
    assert.notEqual(closedText, emptyText, 'refusal and empty-success must differ');
    assert.doesNotMatch(closedText, /暂无定时任务|No scheduled tasks/);
});

test('a refused scheduler request still resolves to a final state', async () => {
    const forbidden = bootTasks({ status: 'error', code: 'forbidden', message: 'not your Agent' });
    await forbidden.ctx.loadTasksView();
    const text = forbidden.get('tasks-empty').querySelector('p').textContent;
    // The server's own reason is shown for a non-closed failure, and the list is
    // not left spinning behind a hidden panel.
    assert.equal(text, 'not your Agent');
    assert.equal(forbidden.get('tasks-list').classList.contains('hidden'), true);
    assert.equal(forbidden.get('tasks-empty').classList.contains('hidden'), false);
});

// =====================================================================
// 4. The page-less slices render the server's refusal
// =====================================================================

// The folder picker (project browse) and the WeChat QR panel are the two
// recovered pages without a console page key: they cannot be gated by the
// projection, so they must render whatever the route answers.
function bootFpBrowse(payload) {
    const nodes = new Map([['folder-picker-list', element()]]);
    const ctx = {
        t: key => key,
        escapeHtml: x => String(x),
        document: { getElementById: id => nodes.get(id) || element() },
        fetch: async () => ({ json: async () => payload }),
        // Only reached on success: the failure path must not need them.
        _fpRenderToolbar: () => {}, _fpRenderList: () => {},
    };
    vm.createContext(ctx);
    vm.runInContext(
        ["const _FP_DRIVES = '__DRIVES__';", fnSource('_fpBrowse')].join('\n'), ctx);
    return { ctx, list: () => nodes.get('folder-picker-list') };
}

test('a refused project browse renders the server reason instead of an empty folder', async () => {
    const { ctx, list } = bootFpBrowse({
        status: 'error', code: 'database_unavailable',
        message: 'unavailable in database identity mode',
    });
    await ctx._fpBrowse('');
    const html = list().innerHTML;
    assert.match(html, /unavailable in database identity mode/,
        'the refusal reason must be rendered');
    assert.doesNotMatch(html, /ws_sel_no_subdirs/,
        'a refusal must not look like an empty folder');
});

test('a browsable folder still renders its entries after the fix-for-refusals', async () => {
    const { ctx, list } = bootFpBrowse({
        status: 'success', path: '~', parent: null,
        dirs: [{ name: 'proj', path: '~/proj' }],
    });
    const rendered = [];
    ctx._fpRenderList = () => rendered.push('list');
    ctx._fpRenderToolbar = () => rendered.push('toolbar');
    await ctx._fpBrowse('');
    assert.deepStrictEqual(rendered, ['toolbar', 'list'], 'the success path is unchanged');
    assert.doesNotMatch(list().innerHTML, /unavailable in database identity mode/);
});

function bootWeixinQr(payload) {
    const panel = element();
    const seen = { rendered: 0, polled: 0 };
    const ctx = {
        t: key => key,
        escapeHtml: x => String(x),
        document: { getElementById: id => (id === 'weixin-qr-panel' ? panel : null) },
        fetch: async () => ({ json: async () => payload }),
        renderWeixinQr: () => { seen.rendered++; },
        startWeixinActiveStatusPoll: () => {},
        pollWeixinQrStatus: () => { seen.polled++; },
    };
    vm.createContext(ctx);
    vm.runInContext(
        ['let _weixinQrPollTimer = null;', fnSource('stopWeixinQrPoll'),
         fnSource('startWeixinQrLogin')].join('\n'), ctx);
    return { ctx, panel, seen };
}

test('a refused wechat qr start renders the server reason instead of a fake qr', async () => {
    const { ctx, panel, seen } = bootWeixinQr({
        status: 'error', code: 'scan_failed', message: 'the scan request failed',
    });
    panel.innerHTML = 'weixin_scan_loading';
    await ctx.startWeixinQrLogin();
    await flush();
    assert.match(panel.innerHTML, /weixin_scan_fail/, 'the failure copy is rendered');
    assert.match(panel.innerHTML, /the scan request failed/, 'the server reason is shown');
    assert.doesNotMatch(panel.innerHTML, /weixin_scan_loading/, 'the spinner is replaced');
    assert.doesNotMatch(panel.innerHTML, /<img/, 'no qr is drawn for a refusal');
    assert.equal(seen.rendered, 0, 'the success renderer must not run');
    assert.equal(seen.polled, 0, 'no scan poll may start for a refusal');
});

test('a started wechat qr still renders the qr and starts polling', async () => {
    const { ctx, panel, seen } = bootWeixinQr({
        status: 'success', source: 'channel', qr_image: 'data:image/png;base64,AA',
    });
    await ctx.startWeixinQrLogin();
    await flush();
    assert.equal(seen.rendered, 1, 'the qr renderer runs on success');
    assert.doesNotMatch(panel.innerHTML, /weixin_scan_fail/);
});

// =====================================================================
// 5. One availability source for the recovered pages
// =====================================================================

test('no module carries its own capability list for the recovered pages', () => {
    // The member personal console was the other console module that rendered its
    // own server projection. It is retired (task 8.8) — the module file, its
    // loader and its i18n namespace are gone — and the recovered pages are the
    // core console's, so a second client-side opinion about them is exactly what
    // this change removes. Asserted on the *shell* now: a re-added loader would
    // be the way a second opinion came back.
    assert.ok(!chatHtml.includes('assets/js/personal-console.js'),
        'the retired personal console must not be loaded again');
    assert.ok(!fs.existsSync(path.join(__dirname, '..',
        'channel/web/static/js/personal-console.js')),
        'the retired personal console module must not come back');
    // The fork fragments the shell mounts carry no availability markup for them.
    const fragments = [...chatHtml.matchAll(/data-fork-fragment="([^"]+)"/g)].map(m => m[1]);
    assert.deepStrictEqual(fragments, ['assets/fragments/appearance-dialog.html']);
});

test('chat.html keeps one not-available surface instead of per-page "not shipped" copy', () => {
    for (const viewId of [...Object.keys(RECOVERED_VIEWS), 'unavailable']) {
        assert.match(chatHtml, new RegExp(`id="view-${viewId}"`),
            `the ${viewId} container must exist`);
    }
    // The "feature not shipped" copy exists once, in the shared placeholder the
    // dispatch routes refusals to — not once per recovered page.
    const occurrences = chatHtml.match(/data-i18n="nav_unavailable"/g) || [];
    assert.equal(occurrences.length, 1);
    const hint = chatHtml.match(/data-i18n="nav_unavailable_hint"/g) || [];
    assert.equal(hint.length, 1);
});

// =====================================================================
// 6. A refused memory read ends in a state the operator can act on
// =====================================================================

function memorySurface() {
    const make = () => ({
        innerHTML: '', textContent: '', className: '',
        classList: (() => {
            const classes = new Set();
            return { add: c => classes.add(c), remove: c => classes.delete(c),
                contains: c => classes.has(c) };
        })(),
    });
    const nodes = {
        'memory-empty': make(), 'memory-list': make(),
        'memory-pagination': make(), 'memory-table-body': make(),
    };
    const icon = make();
    const title = make();
    const hint = make();
    Object.assign(nodes['memory-empty'], {
        querySelector: sel => (sel === 'i' ? icon : title),
        querySelectorAll: sel => (sel === 'p' ? [title, hint] : []),
    });
    return { nodes, icon, title, hint };
}

test('a refused memory read is a terminal state, never an empty folder', () => {
    // The list read used to `return` on a non-success payload, leaving the
    // previous rows (or the "loading" copy) on screen: a 403/503 and "this
    // Agent has no memory files yet" were the same picture, and the reason the
    // server sent was dropped. 9.5 forbids exactly that confusion.
    const toasts = [];
    const surface = memorySurface();
    const sandbox = {
        currentLang: 'zh',
        _wsToast: msg => toasts.push(msg),
        document: { getElementById: id => surface.nodes[id] || null },
    };
    vm.createContext(sandbox);
    vm.runInContext(fnSource('_memoryRefusal'), sandbox);

    const reason = 'memory is not available to this caller';
    const shown = sandbox._memoryRefusal({ status: 'error', code: 'not_owner', message: reason });
    assert.equal(shown, reason, 'the server reason must be what is displayed');
    assert.equal(surface.title.textContent, reason);
    assert.equal(surface.nodes['memory-list'].classList.contains('hidden'), true,
        'stale rows must be cleared, not left behind');
    assert.equal(surface.nodes['memory-empty'].classList.contains('hidden'), false,
        'the refusal must be visible');
    assert.equal(surface.nodes['memory-table-body'].innerHTML, '');
    assert.equal(surface.nodes['memory-pagination'].innerHTML, '');
    assert.ok(surface.icon.className.includes('triangle-exclamation'),
        'a refusal is not the "no files" brain icon');
    assert.notEqual(surface.title.textContent, '暂无记忆文件',
        'a refusal must not be rendered as an empty state');
    assert.deepEqual(toasts, [reason], 'the refusal must also be announced');

    // A payload with no message (a network failure, a non-JSON body) still ends
    // in a terminal state with a copy of its own, in the current language.
    const fallback = sandbox._memoryRefusal(null);
    assert.equal(fallback, '读取记忆失败');
    assert.equal(surface.title.textContent, fallback);

    // One *file* failing to open keeps the list: the list request succeeded, so
    // wiping it would replace a correct answer with an unrelated one.
    surface.nodes['memory-list'].classList.remove('hidden');
    surface.nodes['memory-empty'].classList.add('hidden');
    sandbox._memoryRefusal({ status: 'error', message: 'unknown entry' }, { keepList: true });
    assert.equal(surface.nodes['memory-list'].classList.contains('hidden'), false);
    assert.equal(surface.nodes['memory-empty'].classList.contains('hidden'), true);
});

test('both memory reads route refusals through that surface and swallow nothing', () => {
    const list = fnSource('loadMemoryView');
    const open = fnSource('openMemoryFile');
    assert.match(list, /if \(data\.status !== 'success'\) return _memoryRefusal\(data\);/);
    assert.match(open, /return _memoryRefusal\(data, \{ keepList: true \}\)/);
    // The bare `catch(() => {})` used to hide a failed fetch behind whatever was
    // on screen; both reads must now report it.
    assert.doesNotMatch(list, /\.catch\(\(\) => \{\}\)/);
    assert.doesNotMatch(open, /\.catch\(\(\) => \{\}\)/);
    assert.match(list, /\.catch\(\(\) => _memoryRefusal\(null\)\)/);
    assert.match(open, /\.catch\(\(\) => _memoryRefusal\(null, \{ keepList: true \}\)\)/);
});

test('both navigation layouts share one gate, and a direct link is gated like a click', () => {
    // 9.4 names "两种现有导航布局". The layout switch is presentation only: both
    // configured values (web_navigation_mode classic/split) serve the same
    // path-based shell -- locked separately by tests/test_web_navigation_mode.py
    // -- so the availability gate must not branch on it, or one layout could
    // show a page the other refuses.
    const gate = fnSource('_viewNavDenied');
    assert.doesNotMatch(gate, /_navigationMode\(/,
        'the availability gate must not depend on the layout switch');
    assert.match(gate, /_consolePageForView\(viewId\)/,
        'the gate reads the server page key, not a client list');

    // Every way into a view goes through the same dispatch, so a bookmarked
    // deep link (#view-personal-memory), a pending cross-area view and an
    // in-page click cannot take different paths around the gate.
    const dispatch = fnSource('navigateTo');
    assert.match(dispatch, /const deny = _viewNavDenied\(viewId\);/,
        'the gate must run inside the single view dispatch');
    assert.match(dispatch, /showUnavailableView\(viewId, deny\.reason\)/,
        'a denied view must render the server reason, not load');
    const boot = fnSource('_bootAreaDefaultView');
    assert.match(boot, /navigateTo\(hashView\)/, 'a deep link must go through the dispatch');
    assert.match(boot, /navigateTo\(pending\)/, 'a pending admin view must go through the dispatch');
    assert.match(boot, /navigateTo\(pendingWb\)/, 'a pending workbench view must go through the dispatch');
});
