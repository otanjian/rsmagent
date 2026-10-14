// The console entries must navigate, not open windows -- and must land where the
// address says.
//
// Change ``harden-windows-desktop-reliability``, task 5.2, fixing the reproduced
// difference in docs/design/windows-desktop-verification-2026-10-13.md (item 4):
//
//   「控制台」was `<a href="/admin" target="cow-admin">` and 「工作台」was
//   `<a href="/chat" target="cow-workbench">`, so a click was a *popup*, not
//   in-app navigation -- and the console window landed on `/scheduler` instead
//   of `/admin`.
//
// Two contracts are in play:
//
//  * console-entry-consistency: an entry into an internal page navigates to that
//    page ("首页直接导航动作 SHALL 使用「打开」类文案并跳转获准页面").
//  * console-route-lifecycle: "页面与页签具有稳定地址" and "重复选择同一页面
//    SHALL 不重复添加历史项" -- the address decides the page, and a repeat click
//    adds no window and no history entry.
//
// The mechanism is asserted too, because the *reason* the target attributes were
// wrong is what keeps them from coming back: console.js already switches areas in
// the same window, the desktop container refuses popups outright, and a new
// window inherits a pending view from sessionStorage -- which is where
// `/scheduler` came from.
//
// Run: node --test tests/test_console_entry_named_windows.cjs

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const chatHtml = fs.readFileSync(path.join(root, 'channel/web/chat.html'), 'utf8');
const consoleJs = fs.readFileSync(path.join(root, 'channel/web/static/js/console.js'), 'utf8');
const containerTs = fs.readFileSync(
    path.join(root, 'desktop/src/main/remote/container.ts'), 'utf8');

/** The `<a …>` element with this id, as written in the shell. */
function anchor(id) {
    const match = new RegExp(`<a id="${id}"[\\s\\S]*?>`, 'm').exec(chatHtml);
    assert.ok(match, `${id} is missing from chat.html`);
    return match[0];
}

/**
 * A slice of console.js starting at a marker. Fixed width, not brace matching:
 * the file is checked out with CRLF on Windows, and a brace-shaped regex would
 * fail for reasons that have nothing to do with the behaviour under test.
 */
function regionAt(marker, width = 3000) {
    const start = consoleJs.indexOf(marker);
    assert.ok(start > 0, `${marker} not found in console.js`);
    return consoleJs.slice(start, start + width);
}

test('the two console entries do not open named windows', () => {
    for (const id of ['nav-open-admin', 'nav-return-workbench']) {
        const element = anchor(id);
        assert.ok(!/target\s*=/.test(element),
            `${id} must navigate in the current window, not pop one open`);
    }
});

test('no named target survives anywhere in the console shell', () => {
    assert.ok(!/target="cow-[a-z-]+"/.test(chatHtml),
        'a named target turns an internal entry into a second window');
});

test('the entries keep their registered paths', () => {
    assert.match(anchor('nav-open-admin'), /href="\/admin"/);
    assert.match(anchor('nav-return-workbench'), /href="\/chat"/);
});

test('the click handler switches areas in this window', () => {
    assert.ok(/if \(item\.id === 'nav-open-admin'\) \{\r?\n\s*event\.preventDefault\(\);\r?\n\s*_openNavArea\('admin'\);/
        .test(consoleJs), 'the console entry must preventDefault and switch area');
    assert.ok(/if \(item\.id === 'nav-return-workbench'\) \{\r?\n\s*event\.preventDefault\(\);\r?\n\s*_openNavArea\('workbench'\);/
        .test(consoleJs), 'the workbench entry must preventDefault and switch area');

    const handler = regionAt('function _openNavArea');
    assert.ok(!/window\.open/.test(handler), 'an area switch must never open a window');
    assert.ok(/pushState/.test(handler) || /navigateTo/.test(handler),
        'and it must move this window to the target address');
});

test('a direct /admin load lands on the admin home when nothing is pending', () => {
    const boot = regionAt('function _bootAreaDefaultView', 2000);
    assert.ok(/if \(pending && VIEW_META\[pending\]\) navigateTo\(pending\);\r?\n\s*else navigateTo\('admin-home'\);/
        .test(boot), 'the landing route for /admin is the admin home, not the last pending view');
    assert.ok(/sessionStorage\.removeItem\('cow_admin_pending_view'\)/.test(boot),
        'a pending view must be removed as it is read, so it cannot outlive its switch');
});

test('the desktop container refuses popups, so the shell cannot rely on one', () => {
    assert.match(containerTs, /setWindowOpenHandler\(\(details\) => \{/);
    assert.match(containerTs, /return \{ action: 'deny' \}/,
        'the container denies popups: a named target in the shell would do nothing');
});
