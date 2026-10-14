// Admin-area group gating after the console entry stopped being admin-only
// (change unify-console-by-data-scope, task 3.1).
//
// Removing the tenant_admin-only gate on the 控制台 entry means an ordinary
// member can reach the admin area. The area's *groups* are still rendered from
// the same authoritative projection, and the console must never leave an empty
// 组织与权限 / 模型与接入 heading behind: console-information-architecture requires
// 实际菜单 to show only pages the identity may actually read, and forbids empty
// groups. So a group is visible exactly when at least one of its pages is.
const nodeTest = require('node:test');
const { resolvePython, pythonTest } = require('./_python_bin.cjs');
const PYTHON = resolvePython();
// The authoritative route table comes from Python: skip when there is none.
const test = pythonTest(nodeTest.test);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
// The shipped address module. Loaded into the sandbox when a scenario needs the
// real routing seam, so a stub cannot claim a path is a registered page.
const ROUTER_SOURCE = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/fork/router.js'), 'utf8');

// The authoritative address table the server injects into #console-page-routes
// (channel/web/route_registry.py -> console_page_metadata). Read the production
// source instead of copying a second path list that could drift from it.
let consolePagesCache = null;
function consolePages() {
    if (!consolePagesCache) {
        const python = PYTHON ? PYTHON.path : null;
if (!python) throw new Error('no usable Python interpreter on this host (set PYTHON_BIN)');
        const out = require('node:child_process').execFileSync(python, ['-B', '-c',
            'import json; from channel.web.route_registry import console_page_metadata; '
            + 'print(json.dumps(console_page_metadata()))'],
            { cwd: path.join(__dirname, '..'), encoding: 'utf8' });
        consolePagesCache = JSON.parse(out);
    }
    return consolePagesCache;
}

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
        _items: [],
        getAttribute(k) { return this._attrs[k] === undefined ? null : this._attrs[k]; },
        classList: {
            contains(c) { return classes.has(c); },
            add(c) { classes.add(c); },
            remove(c) { classes.delete(c); },
            toggle(c, on) { if (on) classes.add(c); else classes.delete(c); return on; },
        },
        querySelectorAll(selector) {
            if (selector === '.sidebar-item[data-view]') return this._items;
            return [];
        },
        querySelector(selector) { return (this.querySelectorAll(selector) || [])[0] || null; },
    };
}

// Model the real markup: each `.menu-group` owns its own `.sidebar-item`s, and
// the group element is what carries `sidebar-hidden-admin-area`.
function boot({ ctx = null, isPlatformAdmin = false, groups = [], area = 'workbench',
                pathname = null, windowMembers = null, realAreaFromPath = false,
                consoleRoutes = null } = {}) {
    const allItems = [];
    groups.forEach(g => { g.el._items = g.items; allItems.push(...g.items); });
    const adminGroups = groups.map(g => g.el);
    const platformGroups = groups.filter(g => g.platform).map(g => g.el);
    // A holder, not a captured value: re-running the projection against a changed
    // context is what proves visibility is recomputed rather than accumulated.
    const ctxRef = { value: ctx };
    const selectors = {
        '#sidebar-nav .sidebar-hidden-admin-area': adminGroups,
        '#sidebar-nav .menu-group.sidebar-hidden-admin-area': adminGroups,
        '#sidebar-nav .sidebar-hidden-platform-scope': platformGroups,
        '#sidebar-nav .sidebar-item[data-view]': allItems,
        // The platform boundary is per item now: _applySidebarPermissions hides
        // the `.platform-scope-only` rows (and data-view="platform") through this
        // selector, and the empty-group pass must not count them.
        '#sidebar-nav .sidebar-item': allItems,
    };
    const navOpenAdmin = makeEl();
    // An explicit pathname lets a scenario point at a registered deep address;
    // the legacy `area` calls keep deriving their old /admin or /chat address.
    const resolvedPath = pathname === null ? (area === 'admin' ? '/admin' : '/chat') : pathname;
    // Only routing-available fixtures carry the shipped address table; every
    // other fixture leaves window.ConsoleRoutes absent, which is the production
    // fallback. `window` is a real global _applySidebarPermissions reads.
    const routeNode = consoleRoutes ? { textContent: JSON.stringify(consoleRoutes) } : null;
    const windowObj = { addEventListener() {}, ...(windowMembers || {}) };
    const sandbox = {
        VIEW_META: {
            agents: { console: 'admin.agents' },
            skills: { console: 'admin.skills' },
            memory: { console: 'admin.memory' },
            system_user: { console: 'admin.members' },
            org: { console: 'admin.organization' },
            roles: { console: 'admin.roles' },
            tenant: { console: 'admin.tenants' },
            platform: { console: 'admin.settings' },
            branding: { console: 'admin.branding' },
            logs: { console: 'admin.logs' },
            audit: { console: 'admin.audit' },
            token_usage: { console: 'admin.token_usage' },
        },
        document: {
            getElementById(id) {
                if (id === 'nav-open-admin') return navOpenAdmin;
                if (id === 'console-page-routes') return routeNode;
                return null;
            },
            querySelectorAll(selector) { return selectors[selector] || []; },
            querySelector(selector) { return (selectors[selector] || [])[0] || null; },
        },
        location: {
            pathname: resolvedPath,
            replaced: null,
            replace(url) { this.replaced = url; },
            assign(url) { this.replaced = url; },
        },
        history: { pushState() {}, replaceState() {} },
        window: windowObj,
        sessionStorage: { _m: {}, setItem(k, v) { this._m[k] = v; }, getItem(k) { return this._m[k] || null; } },
        _identityMode: () => 'database',
        _baseAuthContext: () => ctxRef.value,
        _baseAccountSelf: () => ({ user: { is_platform_admin: isPlatformAdmin } }),
        _openNavArea() {},
    };
    // Legacy fixtures keep the area stub; a real-path scenario loads the actual
    // production _navAreaFromPath so the sandbox exercises the shipped seam.
    if (!realAreaFromPath) sandbox._navAreaFromPath = () => area;
    const functions = [fnSource('_consolePageForView'), fnSource('_viewNavDenied'),
        fnSource('_sidebarRecentDenied'), fnSource('_qualifyAdminConsoleEntry'),
        fnSource('_isPlatformOnlyEntry'), fnSource('_applySidebarPermissions')];
    if (realAreaFromPath) functions.push(fnSource('_navAreaFromPath'));
    if (consoleRoutes) functions.push(ROUTER_SOURCE);
    vm.runInNewContext(functions.join('\n'), sandbox);
    return { sandbox, groups, navOpenAdmin, ctxRef, window: windowObj };
}

const READABLE_AGENT = { available: true, read_allowed: true, scope: 'agent', reason: '', actions: {} };
const DENIED_MEMBERS = { available: false, read_allowed: false, scope: 'tenant', reason: 'no_permission', actions: {} };

function agentDevGroup() {
    return {
        el: makeEl({ 'data-group': 'agent-dev' }),
        items: [makeEl({ 'data-view': 'agents' }), makeEl({ 'data-view': 'skills' })],
    };
}

function orgPermGroup() {
    return {
        el: makeEl({ 'data-group': 'org-perm' }),
        items: [makeEl({ 'data-view': 'system_user' }), makeEl({ 'data-view': 'org' }),
                makeEl({ 'data-view': 'roles' })],
    };
}

// A member's projection: 智能体管理 is reachable, 组织与权限 is not.
function memberCtx(extra = {}) {
    return {
        authorization_mode: 'role',
        console_pages: {
            'admin.agents': { ...READABLE_AGENT, scope: 'agent' },
            'admin.members': DENIED_MEMBERS,
            'admin.organization': DENIED_MEMBERS,
            'admin.roles': DENIED_MEMBERS,
            ...extra,
        },
    };
}

test('a member gets the console entry and only the groups holding a reachable page', () => {
    const agentDev = agentDevGroup();
    const orgPerm = orgPermGroup();
    const { sandbox, navOpenAdmin } = boot({ ctx: memberCtx(), groups: [agentDev, orgPerm] });
    sandbox._applySidebarPermissions();
    assert.equal(navOpenAdmin.classList.contains('hidden'), false,
        '控制台 entry must be reachable for a member whose role reaches 智能体管理');
    assert.equal(agentDev.el.classList.contains('hidden'), false,
        '智能体开发 must stay for a member that may read 智能体管理');
    assert.equal(orgPerm.el.classList.contains('hidden'), true,
        '组织与权限 must not leave an empty heading for a member');
});

test('a tenant admin keeps every non-platform group', () => {
    const agentDev = agentDevGroup();
    const orgPerm = orgPermGroup();
    const { sandbox, navOpenAdmin } = boot({
        ctx: {
            authorization_mode: 'role',
            is_tenant_admin: true,
            console_pages: {
                'admin.agents': READABLE_AGENT,
                'admin.skills': { ...READABLE_AGENT, scope: 'agent' },
                'admin.members': { ...READABLE_AGENT, scope: 'tenant' },
                'admin.organization': { ...READABLE_AGENT, scope: 'tenant' },
                'admin.roles': { ...READABLE_AGENT, scope: 'tenant' },
            },
        },
        groups: [agentDev, orgPerm],
    });
    sandbox._applySidebarPermissions();
    assert.equal(navOpenAdmin.classList.contains('hidden'), false);
    assert.equal(agentDev.el.classList.contains('hidden'), false);
    assert.equal(orgPerm.el.classList.contains('hidden'), false);
});

test('an identity with no reachable page gets neither the entry nor its groups', () => {
    const agentDev = agentDevGroup();
    const orgPerm = orgPermGroup();
    const denied = { ...DENIED_MEMBERS, scope: 'agent' };
    const { sandbox, navOpenAdmin } = boot({
        ctx: memberCtx({
            'admin.agents': denied, 'admin.skills': denied, 'admin.memory': denied,
        }),
        groups: [agentDev, orgPerm],
    });
    sandbox._applySidebarPermissions();
    assert.equal(navOpenAdmin.classList.contains('hidden'), true);
    assert.equal(agentDev.el.classList.contains('hidden'), true);
    assert.equal(orgPerm.el.classList.contains('hidden'), true);
});

test('a qualified member opening /admin directly is not bounced back to /chat', () => {
    const { sandbox } = boot({ ctx: memberCtx(), groups: [agentDevGroup()], area: 'admin' });
    sandbox._applySidebarPermissions();
    assert.equal(sandbox.location.replaced, null);
    assert.equal(sandbox.sessionStorage.getItem('cow_nav_admin_denied'), null);
});

test('an unqualified identity opening /admin is still bounced back to /chat', () => {
    const denied = { ...DENIED_MEMBERS, scope: 'agent' };
    const { sandbox } = boot({
        ctx: memberCtx({ 'admin.agents': denied }), groups: [agentDevGroup()], area: 'admin',
    });
    sandbox._applySidebarPermissions();
    assert.equal(sandbox.location.replaced, '/chat');
    assert.equal(sandbox.sessionStorage.getItem('cow_nav_admin_denied'), '1');
});

// ---------------------------------------------------------------------------
// 入口资格被拒: landing vs registered deep paths (change
// restore-console-admin-entry-coverage). These drive the shipped address table
// and the production _navAreaFromPath instead of a stub that always says admin.
// ---------------------------------------------------------------------------

// Without an interpreter the tests in this file are skipped, so the authoritative
// table is never consulted; do not throw at load time in that case.
const PAGES = PYTHON ? consolePages() : null;

// An authenticated member with a known projection, no platform/tenant bypass
// and no reachable 管理区 page: every signed admin page is withheld.
function deniedMemberCtx() {
    const denied = { ...DENIED_MEMBERS, scope: 'agent' };
    return memberCtx({ 'admin.agents': denied, 'admin.skills': denied, 'admin.memory': denied });
}

test('registered admin addresses resolve from the authoritative route table', () => {
    const adminPaths = PAGES.filter(page => page.area === 'admin').map(page => page.path);
    assert.ok(adminPaths.includes('/agents'), '/agents is a registered 管理区 address');
    assert.ok(adminPaths.includes('/settings'), '/settings is a registered 管理区 address');
    assert.equal(PAGES.some(page => page.path === '/admin/agents'), false,
        'no /admin/<page> address is registered');
    const { sandbox } = boot({ pathname: '/agents', realAreaFromPath: true, consoleRoutes: PAGES });
    const routes = sandbox.window.ConsoleRoutes;
    assert.equal(routes.area('/agents'), 'admin');
    assert.equal(routes.area('/settings/models'), 'admin', 'a registered tab address belongs to 管理区');
    const modelsRoute = routes.parse('/settings/models');
    assert.equal(modelsRoute.view, 'config');
    assert.equal(modelsRoute.tab, 'models');
    assert.equal(routes.parse('/admin/agents'), null, 'an unregistered /admin/<page> is not a page');
    assert.equal(routes.area('/admin/agents'), null);
    assert.equal(sandbox._navAreaFromPath('/agents'), 'admin');
    assert.equal(sandbox._navAreaFromPath('/admin/agents'), 'workbench',
        'the production fallback does not infer 管理区 from an unregistered path');
});

test('a denied identity keeps registered admin deep paths and sets the denial marker', () => {
    for (const pathname of ['/agents', '/settings/models']) {
        const agentDev = agentDevGroup();
        const orgPerm = orgPermGroup();
        const { sandbox, navOpenAdmin } = boot({
            ctx: deniedMemberCtx(), groups: [agentDev, orgPerm],
            pathname, realAreaFromPath: true, consoleRoutes: PAGES,
        });
        sandbox._applySidebarPermissions();
        assert.equal(sandbox.location.replaced, null, pathname + ' keeps its address');
        assert.equal(sandbox.sessionStorage.getItem('cow_nav_admin_denied'), '1',
            pathname + ' records the entry denial');
        assert.equal(navOpenAdmin.classList.contains('hidden'), true,
            pathname + ' must not offer the 控制台 entry');
        assert.equal(agentDev.el.classList.contains('hidden'), true);
        assert.equal(orgPerm.el.classList.contains('hidden'), true);
    }
});

test('a denied identity writes the marker and bounces /admin with and without routes', () => {
    for (const consoleRoutes of [PAGES, null]) {
        const { sandbox } = boot({
            ctx: deniedMemberCtx(), groups: [agentDevGroup()],
            pathname: '/admin', realAreaFromPath: true, consoleRoutes,
        });
        sandbox._applySidebarPermissions();
        assert.equal(sandbox.location.replaced, '/chat',
            'the landing page keeps its historical redirect');
        assert.equal(sandbox.sessionStorage.getItem('cow_nav_admin_denied'), '1');
    }
});

test('a qualified member reaches /admin and its granted page without a new denial marker', () => {
    const landing = boot({ ctx: memberCtx(), pathname: '/admin',
        realAreaFromPath: true, consoleRoutes: PAGES });
    landing.sandbox._applySidebarPermissions();
    assert.equal(landing.sandbox.location.replaced, null,
        'a qualified member is not bounced from /admin');
    assert.equal(landing.sandbox.sessionStorage.getItem('cow_nav_admin_denied'), null);
    assert.equal(landing.navOpenAdmin.classList.contains('hidden'), false);

    const deep = boot({ ctx: memberCtx(), pathname: '/agents',
        realAreaFromPath: true, consoleRoutes: PAGES });
    deep.sandbox._applySidebarPermissions();
    assert.equal(deep.sandbox.location.replaced, null);
    assert.equal(deep.sandbox.sessionStorage.getItem('cow_nav_admin_denied'), null);
    assert.equal(deep.navOpenAdmin.classList.contains('hidden'), false);
});

test('without the route module a registered deep path falls back to the workbench', () => {
    const { sandbox } = boot({
        ctx: deniedMemberCtx(), groups: [agentDevGroup()],
        pathname: '/settings/models', realAreaFromPath: true,
    });
    assert.equal(sandbox._navAreaFromPath('/settings/models'), 'workbench',
        'the fallback only recognises /admin, not every registered 管理区 address');
    sandbox._applySidebarPermissions();
    assert.equal(sandbox.location.replaced, null,
        'the entry check does not redirect an address the fallback reads as workbench');
    assert.equal(sandbox.sessionStorage.getItem('cow_nav_admin_denied'), null);
});

test('group visibility is recomputed, never accumulated', () => {
    // Re-running the projection after a grant is withdrawn must hide the group
    // again: the console computes visibility, it does not only ever reveal.
    const agentDev = agentDevGroup();
    const orgPerm = orgPermGroup();
    const { sandbox, ctxRef } = boot({ ctx: memberCtx(), groups: [agentDev, orgPerm] });
    sandbox._applySidebarPermissions();
    assert.equal(agentDev.el.classList.contains('hidden'), false);
    // Withdraw every page of the group and re-run. Both keys are named: an item
    // whose page the projection does not sign is deliberately left visible
    // ("unknown key -> don't guess"), so leaving 工具与技能 out would keep the
    // group reachable and the scenario would not test the recompute at all.
    ctxRef.value = memberCtx({
        'admin.agents': { ...DENIED_MEMBERS, scope: 'agent' },
        'admin.skills': { ...DENIED_MEMBERS, scope: 'agent' },
    });
    sandbox._applySidebarPermissions();
    assert.equal(agentDev.el.classList.contains('hidden'), true);
    assert.equal(orgPerm.el.classList.contains('hidden'), true);
});

// ---------------------------------------------------------------------------
// 平台管理: the platform boundary is per item, not per group
// (change add-audit-and-token-console). The group carries no platform class any
// more, so a tenant administrator reaches it -- and must find only the
// tenant-scoped operator views inside, never 租户管理 (which spans every tenant).
// ---------------------------------------------------------------------------

// What the real backend projects for a tenant administrator
// (auth.service._console_pages_projection): the two operator views are
// available; the four platform-only pages report a *read* grant -- their
// declared permission is "" -- so the per-item gate, not the projection, is what
// has to keep them out of the sidebar.
const PLATFORM_PAGES_FOR_TENANT_ADMIN = {
    'admin.tenants': { available: false, read_allowed: true, scope: 'platform', reason: 'deferred', actions: {} },
    'admin.settings': { available: false, read_allowed: true, scope: 'platform', reason: 'deferred', actions: {} },
    'admin.branding': { available: false, read_allowed: true, scope: 'platform', reason: 'deferred', actions: {} },
    'admin.logs': { available: false, read_allowed: true, scope: 'platform', reason: 'deferred', actions: {} },
    'admin.audit': { available: true, read_allowed: true, scope: 'platform', reason: '', actions: {} },
    'admin.token_usage': { available: true, read_allowed: true, scope: 'platform', reason: '', actions: {} },
};

// 平台用户管理 is no longer a sidebar row (change
// move-password-reset-to-tenant-management), so it is not modelled here: the
// remaining platform-only rows are the ones still painted in chat.html.
const PLATFORM_ONLY_VIEWS = ['tenant', 'branding', 'logs'];

function platformOpsGroup() {
    const item = (view, platformOnly) => {
        const el = makeEl({ 'data-view': view });
        if (platformOnly) el.classList.add('platform-scope-only');
        return el;
    };
    return {
        // No platform class on the group element: that is the point of the change.
        el: makeEl({ 'data-group': 'platform-ops' }),
        items: [...PLATFORM_ONLY_VIEWS.map(v => item(v, true)),
                item('audit', false), item('token_usage', false)],
    };
}

const offered = item => !item.classList.contains('hidden');

function itemsByView(group) {
    return Object.fromEntries(group.items.map(i => [i.getAttribute('data-view'), i]));
}

test('a platform admin gets every 平台管理 entry', () => {
    const group = platformOpsGroup();
    const { sandbox } = boot({
        ctx: { authorization_mode: 'all', console_pages: PLATFORM_PAGES_FOR_TENANT_ADMIN },
        isPlatformAdmin: true,
        groups: [group],
    });
    sandbox._applySidebarPermissions();
    assert.equal(group.el.classList.contains('hidden'), false);
    for (const [view, item] of Object.entries(itemsByView(group))) {
        assert.ok(offered(item), `${view} must be offered to a platform admin`);
    }
});

test('a tenant admin gets 平台管理 with only the two operator views', () => {
    const group = platformOpsGroup();
    const { sandbox } = boot({
        ctx: {
            authorization_mode: 'role',
            is_tenant_admin: true,
            console_pages: PLATFORM_PAGES_FOR_TENANT_ADMIN,
        },
        groups: [group],
    });
    sandbox._applySidebarPermissions();
    assert.equal(group.el.classList.contains('hidden'), false,
        '审计日志 / Token 消耗 must keep the 平台管理 heading alive for a tenant admin');
    const byView = itemsByView(group);
    assert.ok(offered(byView.audit), '审计日志 must be offered to a tenant admin');
    assert.ok(offered(byView.token_usage), 'Token 消耗 must be offered to a tenant admin');
    for (const view of PLATFORM_ONLY_VIEWS) {
        assert.equal(offered(byView[view]), false,
            `${view} is platform-only and must not reach a tenant admin`);
    }
});

test('an ordinary member gets no 平台管理 heading at all', () => {
    // The projection refuses both operator views for a member, and the four
    // platform-only rows must not be what keeps the heading on screen -- the
    // empty-group rule has to hide it.
    const group = platformOpsGroup();
    const denied = { ...DENIED_MEMBERS, scope: 'platform' };
    const { sandbox } = boot({
        ctx: {
            authorization_mode: 'role',
            console_pages: {
                ...PLATFORM_PAGES_FOR_TENANT_ADMIN,
                'admin.audit': denied, 'admin.token_usage': denied,
            },
        },
        groups: [group],
    });
    sandbox._applySidebarPermissions();
    assert.equal(group.el.classList.contains('hidden'), true,
        'an empty 平台管理 heading must not be left behind for a member');
});
