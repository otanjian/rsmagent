// Workbench menu-grant gating for the nested 会话历史 block.
//
// A role with an explicit menu grant set that omits `nav:workbench.history` must
// not see the 会话历史 entry, even though it still holds the functional
// `history.read` permission. The server marks such a page `menu_denied`; the nav
// helpers must honour that signal for workbench pages too (previously only
// `admin.*` pages were gated) and skip the recent-sessions request.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');

// Extract one top-level function by brace matching (the navigation section has
// many neighbours and `section()` boundaries would drag in unrelated deps).
function fnSource(name) {
    const head = `function ${name}(`;
    const from = source.indexOf(head);
    assert.ok(from >= 0, `Missing ${name}`);
    let depth = 0;
    for (let i = source.indexOf('{', from); i < source.length; i++) {
        if (source[i] === '{') depth++;
        else if (source[i] === '}') {
            depth--;
            if (depth === 0) return source.slice(from, i + 1);
        }
    }
    throw new Error(`Unbalanced ${name}`);
}

function makeEl(attrs = {}) {
    const classes = new Set();
    return {
        _attrs: { ...attrs },
        getAttribute(k) { return this._attrs[k] === undefined ? null : this._attrs[k]; },
        classList: {
            _set: classes,
            contains(c) { return classes.has(c); },
            add(c) { classes.add(c); },
            remove(c) { classes.delete(c); },
            toggle(c, on) { if (on) classes.add(c); else classes.delete(c); return on; },
        },
        hidden: classes.has('hidden'),
    };
}

function boot({ mode = 'database', ctx = null, isPlatformAdmin = false, items = [], area = 'workbench' } = {}) {
    const adminAreaEls = [makeEl()];
    const platformScopeEls = [makeEl()];
    const platformItem = makeEl({ 'data-view': 'platform' });
    const recentEl = makeEl();
    const navOpenAdmin = makeEl();
    const selectors = {
        '#sidebar-nav .sidebar-hidden-admin-area': adminAreaEls,
        '#sidebar-nav .sidebar-hidden-platform-scope': platformScopeEls,
        '#sidebar-nav .sidebar-item[data-view]': items,
        '.sidebar-item[data-view="platform"]': [platformItem],
    };
    const sandbox = {
        VIEW_META: {
            history: { console: 'workbench.history' },
            channels: { console: 'admin.channels' },
        },
        document: {
            getElementById(id) {
                if (id === 'nav-open-admin') return navOpenAdmin;
                if (id === 'sidebar-recent') return recentEl;
                return null;
            },
            querySelectorAll(selector) { return selectors[selector] || []; },
            querySelector(selector) { return (selectors[selector] || [])[0] || null; },
        },
        location: { pathname: `/${area}`, replace() {}, assign() {} },
        sessionStorage: { setItem() {}, getItem() { return null; } },
        _identityMode: () => mode,
        _baseAuthContext: () => ctx,
        _baseAccountSelf: () => ({ user: { is_platform_admin: isPlatformAdmin } }),
        _navAreaFromPath: () => area,
        _openNavArea() {},
        // Hosted by the account panel now; this slice only calls into it.
        _renderAccountResources() {},
        _syncAccountPersonalCurrent() {},
    };
    vm.runInNewContext(
        [fnSource('_consolePageForView'), fnSource('_viewNavDenied'),
         fnSource('_sidebarRecentDenied'), fnSource('_qualifyAdminConsoleEntry'),
         fnSource('_applySidebarPermissions')].join('\n'),
        sandbox);
    return { sandbox, recentEl, items };
}

const DENIED = { available: false, read_allowed: false, scope: 'self', reason: 'menu_not_granted', actions: {}, menu_denied: true };
const ALLOWED = { available: false, read_allowed: true, scope: 'self', reason: 'consumer_closed', actions: {} };

test('a withheld menu grant denies workbench.history and hides the block', () => {
    const { sandbox, recentEl } = boot({
        ctx: { console_pages: { 'workbench.history': DENIED }, authorization_mode: 'role' },
    });
    sandbox._applySidebarPermissions();
    const denied = sandbox._viewNavDenied('history');
    assert.ok(denied && denied.reason === 'denied', JSON.stringify(denied));
    assert.equal(recentEl.classList.contains('hidden'), true);
});

test('a role with no menu grant keeps the functional-permission behaviour', () => {
    const { sandbox, recentEl } = boot({
        // Compat: no menu_denied flag -> the page is not blocked by the menu rule.
        ctx: { console_pages: { 'workbench.history': ALLOWED }, authorization_mode: 'role' },
    });
    sandbox._applySidebarPermissions();
    assert.equal(sandbox._viewNavDenied('history'), null);
    assert.equal(recentEl.classList.contains('hidden'), false);
});

test('platform "all" is never restricted by a menu denial', () => {
    const { sandbox, recentEl } = boot({
        isPlatformAdmin: true,
        ctx: { console_pages: { 'workbench.history': DENIED }, authorization_mode: 'all' },
    });
    sandbox._applySidebarPermissions();
    assert.equal(sandbox._viewNavDenied('history'), null);
    assert.equal(recentEl.classList.contains('hidden'), false);
});

test('an unknown projection never hides or blocks the block', () => {
    const { sandbox, recentEl } = boot({ ctx: null });
    sandbox._applySidebarPermissions();
    assert.equal(sandbox._viewNavDenied('history'), null);
    assert.equal(recentEl.classList.contains('hidden'), false);
});

test('legacy mode never hides or blocks the block', () => {
    const { sandbox, recentEl } = boot({ mode: 'legacy', ctx: null });
    sandbox._applySidebarPermissions();
    assert.equal(sandbox._viewNavDenied('history'), null);
    assert.equal(recentEl.classList.contains('hidden'), false);
});

test('the recent-sessions request is skipped when the grant is withheld', () => {
    let fetched = 0;
    const recentEl = makeEl();
    const sandbox = {
        document: { getElementById: (id) => (id === 'sidebar-recent' ? recentEl : null) },
        location: { pathname: '/chat' },
        _navAreaFromPath: () => 'workbench',
        _sidebarRecentSeq: 0,
        _sidebarRecentDenied: () => true,
        SIDEBAR_RECENT_LIMIT: 10,
        sidebarRecentLimitCount: () => 10,
        _sidebarRecentLimit: (items) => items,
        renderSidebarRecentSessions: () => {},
        t: (k) => k,
        fetch: () => { fetched++; return Promise.resolve({ ok: true, json: async () => ({ status: 'success', sessions: [] }) }); },
    };
    vm.runInNewContext(fnSource('loadSidebarRecentSessions'), sandbox);
    sandbox.loadSidebarRecentSessions();
    assert.equal(fetched, 0);
    assert.equal(recentEl.classList.contains('hidden'), true);
});

test('the recent-sessions request runs when the block is visible', () => {
    let fetched = 0;
    const recentEl = makeEl();
    const sandbox = {
        document: { getElementById: (id) => (id === 'sidebar-recent' ? recentEl : null) },
        location: { pathname: '/chat' },
        _navAreaFromPath: () => 'workbench',
        _sidebarRecentSeq: 0,
        _sidebarRecentDenied: () => false,
        SIDEBAR_RECENT_LIMIT: 10,
        // The preview page size is a presentation value owned by
        // `sidebarRecentLimitCount`; the sandbox pins the pre-switch answer.
        sidebarRecentLimitCount: () => 10,
        _sidebarRecentLimit: (items) => items,
        renderSidebarRecentItems() {},
        renderSidebarRecentSessions: () => {},
        t: (k) => k,
        fetch: () => { fetched++; return Promise.resolve({ ok: true, json: async () => ({ status: 'success', sessions: [] }) }); },
    };
    vm.runInNewContext(fnSource('loadSidebarRecentSessions'), sandbox);
    sandbox.loadSidebarRecentSessions();
    assert.equal(fetched, 1);
});

test('chat.html keeps the recent block addressable without a competing data-view', () => {
    const html = fs.readFileSync(path.join(__dirname, '../channel/web/chat.html'), 'utf8');
    assert.match(html, /id="sidebar-recent"/);
    assert.match(html, /id="sidebar-recent-list"/);
    // The top-level `history` menu item was folded into this nested block, so it
    // must not come back as a `data-view="history"` item.
    const historyItem = html.indexOf('data-view="history"');
    assert.equal(historyItem, -1, 'top-level history menu item must stay removed');
});

test('the recent block has a CSS rule that honours the hidden class', () => {
    const css = fs.readFileSync(path.join(__dirname, '../channel/web/static/css/console.css'), 'utf8');
    assert.match(css, /\.sidebar-recent\.hidden\s*\{[^}]*display:\s*none/);
});
