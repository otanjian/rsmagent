// Path-based workbench/admin nav helpers (chat/admin split).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');

function section(start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from + start.length);
    assert.ok(from >= 0 && to > from, `Missing console section ${start}`);
    return source.slice(from, to + end.length);
}

test('nav area helpers: path, open, qualify entry', () => {
    const code = section('// === NAV_AREA_BEGIN ===', '// === NAV_AREA_END ===');
    const appEl = {
        _attrs: {},
        setAttribute(k, v) { this._attrs[k] = v; },
    };
    const location = { pathname: '/chat' };
    const sandbox = {
        window: {
            history: {
                pushState(state, title, url) {
                    sandbox._pushed = { state, url };
                    location.pathname = url;
                },
            },
            addEventListener() {},
        },
        document: {
            getElementById(id) { return id === 'app' ? appEl : null; },
        },
        location,
        _bootAreaDefaultView() { sandbox._boots = (sandbox._boots || 0) + 1; },
    };
    vm.runInNewContext(code, sandbox);
    assert.equal(sandbox._navAreaFromPath('/chat'), 'workbench');
    assert.equal(sandbox._navAreaFromPath('/admin'), 'admin');
    assert.equal(sandbox._navAreaFromPath('/admin/'), 'admin');
    assert.equal(sandbox._navAreaFromPath('/admin?x=1'.split('?')[0]), 'admin');
    sandbox._openNavArea('admin');
    assert.equal(sandbox._pushed.url, '/admin');
    assert.equal(sandbox._pushed.state.cowArea, 'admin');
    assert.equal(appEl._attrs['data-nav-area'], 'admin');
    assert.equal(sandbox._boots, 1);
    sandbox._openNavArea('workbench');
    assert.equal(sandbox._pushed.url, '/chat');
    assert.equal(sandbox._pushed.state.cowArea, 'workbench');
    assert.equal(appEl._attrs['data-nav-area'], 'workbench');
    assert.equal(sandbox._boots, 2);
    assert.equal(sandbox._qualifyAdminConsoleEntry({
        identityMode: 'database', isPlatformAdmin: false, isTenantAdmin: true,
    }), true);
    assert.equal(sandbox._qualifyAdminConsoleEntry({
        identityMode: 'database', isPlatformAdmin: true, isTenantAdmin: false,
    }), true);
    assert.equal(sandbox._qualifyAdminConsoleEntry({
        identityMode: 'legacy', isPlatformAdmin: false, isTenantAdmin: false,
    }), true);
});

// The 控制台 entry is no longer tenant_admin-only (change
// unify-console-by-data-scope, task 3.1): admission is the trusted formal-page
// projection, so an ordinary member whose role reaches a business page gets the
// entry. 组织与权限 and the platform surface keep their own management checks —
// via their page keys and the platform-scope shell flag — not via this gate.
test('console entry qualification follows the formal page projection', () => {
    const code = section('// === NAV_AREA_BEGIN ===', '// === NAV_AREA_END ===');
    const sandbox = { window: { addEventListener() {} }, document: { getElementById() { return null; } },
                      location: { pathname: '/chat' } };
    vm.runInNewContext(code, sandbox);
    const qualify = (opts) => sandbox._qualifyAdminConsoleEntry(opts);

    // Unknown projection: don't guess / don't block, same rule as _viewNavDenied.
    assert.equal(qualify({ identityMode: 'database' }), true);
    assert.equal(qualify({ identityMode: 'database', pages: null }), true);

    // A readable business page admits an ordinary member.
    assert.equal(qualify({
        identityMode: 'database',
        pages: { 'admin.agents': { available: true, read_allowed: true, scope: 'agent' } },
    }), true);

    // Nothing readable: the entry stays hidden rather than opening an empty shell.
    assert.equal(qualify({
        identityMode: 'database',
        pages: { 'admin.agents': { available: false, read_allowed: false, scope: 'agent' } },
    }), false);

    // Platform-scope pages are not business entry points: they never admit on
    // their own, so a member cannot reach the console through 平台管理.
    assert.equal(qualify({
        identityMode: 'database',
        pages: { 'admin.logs': { available: true, read_allowed: true, scope: 'platform' } },
    }), false);

    // A withheld menu grant withholds the entry too.
    assert.equal(qualify({
        identityMode: 'database',
        pages: { 'admin.agents': { available: true, read_allowed: true, scope: 'agent', menu_denied: true } },
    }), false);

    // A capability the deployment withdrew is not an admission either: the
    // per-item gate hides it, so counting it here would open an empty console.
    assert.equal(qualify({
        identityMode: 'database',
        pages: { 'admin.agents': { available: false, read_allowed: true, scope: 'agent', reason: 'capability_disabled' } },
    }), false);

    // Workbench and personal pages are not reachable from the console shell, so
    // they must not open a shell with nothing in it: the 管理区 entry is admitted
    // by 管理区 pages only (console-information-architecture: 没有管理区可访问页面时
    // 隐藏该区域入口).
    assert.equal(qualify({
        identityMode: 'database',
        pages: { 'workbench.todos': { available: true, read_allowed: true, scope: 'self' } },
    }), false);
    assert.equal(qualify({
        identityMode: 'database',
        pages: {
            'workbench.todos': { available: true, read_allowed: true, scope: 'self' },
            'personal.agents': { available: true, read_allowed: true, scope: 'self' },
        },
    }), false);
});

test('chat.html has area markers and admin home', () => {
    const htmlSource = fs.readFileSync(path.join(__dirname, '../channel/web/chat.html'), 'utf8');
    assert.match(htmlSource, /id="nav-open-admin"/);
    assert.match(htmlSource, /id="nav-return-workbench"/);
    assert.match(htmlSource, /id="nav-admin-home"/);
    assert.match(htmlSource, /data-view="admin-home"/);
    assert.match(htmlSource, /id="view-admin-home"/);
    assert.match(htmlSource, /data-nav-shell="workbench"/);
    assert.match(htmlSource, /data-nav-shell="admin"/);
});

test('chat.html pins admin entry above account footer', () => {
    const htmlSource = fs.readFileSync(path.join(__dirname, '../channel/web/chat.html'), 'utf8');
    const adminIdx = htmlSource.indexOf('id="nav-open-admin"');
    const workbenchIdx = htmlSource.indexOf('id="nav-return-workbench"');
    const footerIdx = htmlSource.indexOf('id="sidebar-account-footer"');
    const navOpen = htmlSource.indexOf('id="sidebar-nav"');
    const navClose = htmlSource.indexOf('</nav>', navOpen);
    assert.ok(adminIdx > 0 && footerIdx > adminIdx, 'nav-open-admin must sit above sidebar-account-footer');
    assert.ok(workbenchIdx > 0 && footerIdx > workbenchIdx, 'nav-return-workbench must sit above sidebar-account-footer');
    assert.match(htmlSource, /sidebar-admin-entry-wrap/);
    assert.ok(adminIdx > navClose, 'admin entry must be outside scrolling sidebar-nav');
    assert.ok(workbenchIdx > navClose, 'workbench entry must be outside scrolling sidebar-nav');
});

test('chat.html pins recent sessions under scenes', () => {
    const htmlSource = fs.readFileSync(path.join(__dirname, '../channel/web/chat.html'), 'utf8');
    const scenesIdx = htmlSource.indexOf('data-view="scenes"');
    const recentIdx = htmlSource.indexOf('id="sidebar-recent"');
    const historyItem = htmlSource.indexOf('data-view="history"');
    assert.ok(scenesIdx > 0, 'scenes menu item exists');
    assert.ok(recentIdx > scenesIdx, 'sidebar-recent sits after scenes');
    assert.equal(historyItem, -1, 'top-level history menu item is removed');
    assert.match(htmlSource, /id="sidebar-recent-list"/);
    assert.match(htmlSource, /id="sidebar-recent-label"/);
    assert.match(htmlSource, /fa-clock-rotate-left/);
    assert.match(htmlSource, /会话历史/);
});

test('sidebar recent sessions keep at most 10', () => {
    const code = section('// === SIDEBAR_RECENT_BEGIN ===', '// === SIDEBAR_RECENT_END ===');
    const sandbox = {};
    vm.runInNewContext(code, sandbox);
    const items = Array.from({ length: 15 }, (_, i) => ({ session_id: 's' + i, title: 't' + i }));
    assert.equal(sandbox._sidebarRecentLimit(items).length, 10);
    assert.equal(sandbox._sidebarRecentLimit(items)[0].session_id, 's0');
    assert.equal(sandbox._sidebarRecentLimit(items.slice(0, 3)).length, 3);
});
