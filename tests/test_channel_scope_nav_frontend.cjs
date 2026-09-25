// Console-scope navigation contract for the 消息渠道 page (tenant-owned-message-channels 8.5).
//
// One page key serves two scopes: the server reports `scope: "platform"` for a
// platform admin and `"tenant"` for a tenant admin, both available. The nav
// helpers must show the entry for whichever scope the operator owns, and hide
// it when the projection withholds it — never guessing on a missing projection.
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

function boot({ mode = 'database', ctx = null, isPlatformAdmin = false, items = [], area = 'admin' } = {}) {
    const adminAreaEls = [makeEl()];
    const platformScopeEls = [makeEl()];
    const platformItem = makeEl({ 'data-view': 'platform' });
    const navOpenAdmin = makeEl();
    const selectors = {
        '#sidebar-nav .sidebar-hidden-admin-area': adminAreaEls,
        '#sidebar-nav .sidebar-hidden-platform-scope': platformScopeEls,
        '#sidebar-nav .sidebar-item[data-view]': items,
        '.sidebar-item[data-view="platform"]': [platformItem],
    };
    // The five 「我的」 entries are retired (task 3.3), so the per-item gate owns
    // the whole verdict: it must not hand anything to an account-panel projector
    // that no longer exists. That section is exercised on its own in
    // test_sidebar_account_frontend.cjs.
    const sandbox = {
        VIEW_META: { channels: { console: 'admin.channels' } },
        document: {
            getElementById(id) { return id === 'nav-open-admin' ? navOpenAdmin : null; },
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
    };
    vm.runInNewContext(
        [fnSource('_consolePageForView'), fnSource('_viewNavDenied'),
         fnSource('_qualifyAdminConsoleEntry'), fnSource('channelPageScope'),
         fnSource('channelScope'),
         fnSource('_applySidebarPermissions')].join('\n'),
        sandbox);
    return { sandbox, channelItem: items[0], adminAreaEls, platformScopeEls, navOpenAdmin };
}

const TENANT_PAGE = { available: true, read_allowed: true, scope: 'tenant', reason: '', actions: { create: true, update: true } };
const PLATFORM_PAGE = { available: true, read_allowed: true, scope: 'platform', reason: '', actions: { create: true, update: true } };
const SELF_PAGE = { available: true, read_allowed: true, scope: 'self', reason: '', actions: { create: true, update: true } };

test('the channels view maps to the single admin.channels page key', () => {
    const { sandbox } = boot();
    assert.equal(sandbox._consolePageForView('channels'), 'admin.channels');
    assert.equal(sandbox._consolePageForView('view-channels'), '');
});

test('a tenant admin sees its own tenant-scoped channels entry', () => {
    const item = makeEl({ 'data-view': 'channels' });
    const { sandbox, channelItem } = boot({
        ctx: { console_pages: { 'admin.channels': TENANT_PAGE }, is_tenant_admin: true, authorization_mode: 'role' },
        items: [item],
    });
    sandbox._applySidebarPermissions();
    assert.equal(channelItem.classList.contains('hidden'), false);
    assert.equal(sandbox._viewNavDenied('channels'), null);
});

test('a platform admin sees the platform-scoped channels entry', () => {
    const item = makeEl({ 'data-view': 'channels' });
    const { sandbox, channelItem } = boot({
        isPlatformAdmin: true,
        ctx: { console_pages: { 'admin.channels': PLATFORM_PAGE }, is_tenant_admin: false, authorization_mode: 'all' },
        items: [item],
    });
    sandbox._applySidebarPermissions();
    assert.equal(channelItem.classList.contains('hidden'), false);
    assert.equal(sandbox._viewNavDenied('channels'), null);
});

test('a page the projection withholds is denied and hidden', () => {
    const item = makeEl({ 'data-view': 'channels' });
    const withheld = { available: false, read_allowed: false, scope: 'tenant', reason: 'no_tenant_control', actions: { create: false, update: false } };
    const { sandbox, channelItem } = boot({
        // Still qualified for /admin, so this isolates the per-item gate rather
        // than the area-entry gate.
        ctx: { console_pages: { 'admin.channels': withheld }, is_tenant_admin: true, authorization_mode: 'role' },
        items: [item],
    });
    sandbox._applySidebarPermissions();
    assert.equal(channelItem.classList.contains('hidden'), true);
    // Cross-realm object: compare the field, not the prototype.
    const denied = sandbox._viewNavDenied('channels');
    assert.ok(denied && denied.reason === 'denied', JSON.stringify(denied));
});

test('a missing projection never hides or blocks the page', () => {
    const item = makeEl({ 'data-view': 'channels' });
    const { sandbox, channelItem } = boot({ ctx: null, items: [item] });
    sandbox._applySidebarPermissions();
    assert.equal(channelItem.classList.contains('hidden'), false);
    assert.equal(sandbox._viewNavDenied('channels'), null);
});

test('legacy mode and platform "all" mode are never blocked by the projection', () => {
    const legacy = boot({ mode: 'legacy', ctx: null });
    assert.equal(legacy.sandbox._viewNavDenied('channels'), null);
    const all = boot({ ctx: { console_pages: {}, authorization_mode: 'all' } });
    assert.equal(all.sandbox._viewNavDenied('channels'), null);
});

test('a member sees the channels entry scoped to their own connections', () => {
    // Task 6.1: the same page key answers with `scope: "self"` for a member, and
    // the nav gate must admit it — the entry goes to the tenant business surface,
    // where the interface answers with their own range.
    const item = makeEl({ 'data-view': 'channels' });
    const { sandbox, channelItem } = boot({
        ctx: { console_pages: { 'admin.channels': SELF_PAGE }, is_tenant_admin: false, authorization_mode: 'role' },
        items: [item],
    });
    sandbox._applySidebarPermissions();
    assert.equal(channelItem.classList.contains('hidden'), false);
    assert.equal(sandbox._viewNavDenied('channels'), null);
});

test('the member scope loads the business surface, not the instance page', () => {
    // `self` is the *same* page as a tenant admin's: the platform instance list
    // is a different surface and a member must never be handed it (task 6.1).
    const { sandbox } = boot({
        ctx: { console_pages: { 'admin.channels': SELF_PAGE }, is_tenant_admin: false, authorization_mode: 'role' },
    });
    assert.equal(sandbox.channelPageScope(), 'self');
    assert.equal(sandbox.channelScope(), 'tenant');
});

test('the per-item gate does not hand the verdict to an account-panel projector', () => {
    // Task 3.3 retired the account panel's personal entries. The sidebar gate must
    // therefore be self-contained: one projector owns the projection, and it is
    // this one. A re-introduced hand-off would mean a second verdict exists.
    const { sandbox } = boot({
        ctx: { console_pages: {}, is_tenant_admin: true, authorization_mode: 'role' },
    });
    sandbox._applySidebarPermissions();
    assert.equal(typeof sandbox._renderAccountResources, 'undefined');
    assert.equal(typeof sandbox._syncAccountPersonalCurrent, 'undefined');
});

test('the sidebar entry still exists in chat.html for both admin scopes', () => {
    const html = fs.readFileSync(path.join(__dirname, '../channel/web/chat.html'), 'utf8');
    assert.match(html, /data-view="channels"/);
    // The enclosing group is admin-area gated and must NOT be hard-coded to the
    // platform scope, or a tenant admin would lose the entry.
    const group = html.slice(0, html.indexOf('data-view="channels"'));
    // Match the group container itself, not the nested `menu-group-items` list.
    const tagStart = group.lastIndexOf('class="menu-group ');
    assert.ok(tagStart >= 0, 'channels entry is not inside a menu-group');
    const groupTag = group.slice(tagStart, group.indexOf('>', tagStart));
    assert.ok(groupTag.includes('sidebar-hidden-admin-area'), groupTag);
    assert.ok(!groupTag.includes('sidebar-hidden-platform-scope'), groupTag);
});

test('平台管理 gates its platform-only rows per item, not per group', () => {
    // The group must stay reachable for a tenant administrator, because it now
    // holds 审计日志 / Token 消耗. So the platform boundary cannot live on the
    // group: that would either hide all six rows from them, or -- if simply
    // dropped -- expose 租户管理, which spans every tenant. It lives on the four
    // platform-only rows instead. See _isPlatformOnlyEntry in console.js.
    const html = fs.readFileSync(path.join(__dirname, '../channel/web/chat.html'), 'utf8');
    const groupStart = html.indexOf('data-group="platform-ops"');
    assert.ok(groupStart >= 0, 'the 平台管理 group exists');
    const tagStart = html.lastIndexOf('class="menu-group ', groupStart);
    const groupTag = html.slice(tagStart, html.indexOf('>', tagStart));
    assert.ok(groupTag.includes('sidebar-hidden-admin-area'), groupTag);
    assert.ok(!groupTag.includes('sidebar-hidden-platform-scope'),
        `平台管理 must not be platform-gated as a group: ${groupTag}`);

    const body = html.slice(html.indexOf('>', groupStart),
                            html.indexOf('</nav>', groupStart));
    const rowFor = view => {
        const at = body.indexOf(`data-view="${view}"`);
        assert.ok(at >= 0, `${view} row exists inside 平台管理`);
        return body.slice(body.lastIndexOf('<a ', at), body.indexOf('>', at));
    };
    for (const view of ['tenant', 'platform', 'branding', 'logs']) {
        assert.ok(rowFor(view).includes('platform-scope-only'),
            `${view} is platform-only and must carry the per-item marker`);
    }
    for (const view of ['audit', 'token_usage']) {
        assert.ok(!rowFor(view).includes('platform-scope-only'),
            `${view} must stay reachable below platform scope`);
    }
});
