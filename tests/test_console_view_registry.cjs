// View registry contract (change fork-decoupling-and-tenant-hardening, task 8.6).
//
// console.js used to hard-code the fork admin/TODO view loaders in navigation
// dispatch. Fork modules now register { id, label, load, repaint } on a registry
// and console.js iterates it, so the upstream core file carries no fork-only
// view branches and a fork view can be added without editing it.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const read = p => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const consoleJs = read('channel/web/static/js/console.js');
const identityAdmin = read('channel/web/static/js/identity-admin.js');
const auditConsole = read('channel/web/static/js/audit-console.js');
const todos = read('channel/web/static/js/todos.js');

const FORK_LOADERS = ['loadPlatformUsersView', 'loadTenantView', 'loadMembersView',
    'loadRolesView', 'loadOrgView', 'loadTodosView'];
const FORK_VIEW_IDS = ['platform', 'tenant', 'system_user', 'roles', 'org'];
// 审计日志 / Token 消耗 live in their own module because they read this fork's
// centralized `identity.db` tables instead of identity-admin's per-tenant caches.
const AUDIT_CONSOLE_VIEW_IDS = ['audit', 'token_usage'];

function extractFunction(src, name) {
    const head = `function ${name}(`;
    const from = src.indexOf(head);
    assert.ok(from >= 0, `Missing ${name} in console.js`);
    let depth = 0;
    for (let i = src.indexOf('{', from); i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') {
            depth--;
            if (depth === 0) return src.slice(from, i + 1);
        }
    }
    throw new Error(`Unbalanced ${name}`);
}

test('console.js exposes the registry and iterates it in dispatch', () => {
    assert.match(consoleJs, /function registerConsoleView\(spec\)/);
    assert.match(consoleJs, /window\.registerConsoleView = registerConsoleView/);
    assert.match(consoleJs, /_loadRegisteredView\(viewId\)/);
    assert.match(consoleJs, /_repaintRegisteredView\(currentView\)/);
});

test('console.js carries no fork-only view loader references', () => {
    for (const name of FORK_LOADERS) {
        assert.ok(!new RegExp('\\b' + name + '\\b').test(consoleJs),
            `console.js still references the fork loader ${name}`);
    }
});

test('the registry actually drives load and repaint', () => {
    const source = [
        'const CONSOLE_VIEW_REGISTRY = [];',
        extractFunction(consoleJs, 'registerConsoleView'),
        extractFunction(consoleJs, '_registeredConsoleView'),
        extractFunction(consoleJs, '_activateViewContainer'),
        extractFunction(consoleJs, '_loadRegisteredView'),
        extractFunction(consoleJs, '_repaintRegisteredView'),
    ].join('\n');
    const sandbox = { currentView: 'sample', document: {
        querySelectorAll: () => [],
        getElementById: () => null,
    } };
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);

    const seen = [];
    sandbox.registerConsoleView({
        id: 'sample',
        label: 'menu_sample',
        load: () => seen.push('load'),
        repaint: () => seen.push('repaint'),
    });
    sandbox._loadRegisteredView('sample');
    sandbox._repaintRegisteredView('sample');
    // Unknown ids (and specs without the callback) are no-ops.
    sandbox._loadRegisteredView('nope');
    sandbox._repaintRegisteredView('nope');
    sandbox.registerConsoleView({ id: 'load-only', load: () => seen.push('load-only') });
    sandbox._repaintRegisteredView('load-only');
    sandbox._loadRegisteredView('load-only');
    assert.deepStrictEqual(seen, ['load', 'repaint', 'load-only']);

    // Re-registering the same id replaces it instead of duplicating.
    sandbox.registerConsoleView({ id: 'sample', load: () => seen.push('load2') });
    sandbox._loadRegisteredView('sample');
    sandbox.registerConsoleView({ id: 'sample', load: () => seen.push('load3') });
    sandbox._loadRegisteredView('sample');
    assert.deepStrictEqual(seen, ['load', 'repaint', 'load-only', 'load2', 'load3']);
});

test('an empty registry degrades to a no-op (plain upstream navigation)', () => {
    const source = [
        'const CONSOLE_VIEW_REGISTRY = [];',
        extractFunction(consoleJs, 'registerConsoleView'),
        extractFunction(consoleJs, '_registeredConsoleView'),
        extractFunction(consoleJs, '_loadRegisteredView'),
        extractFunction(consoleJs, '_repaintRegisteredView'),
    ].join('\n');
    const sandbox = {};
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);

    // With nothing registered, dispatch must be a silent no-op — never a throw
    // — so an upstream build (no fork modules) still navigates normally.
    assert.doesNotThrow(() => sandbox._loadRegisteredView('tenant'));
    assert.doesNotThrow(() => sandbox._repaintRegisteredView('todo'));
    // Malformed registrations are ignored rather than crashing the page.
    assert.doesNotThrow(() => sandbox.registerConsoleView(null));
    assert.doesNotThrow(() => sandbox.registerConsoleView({}));
    assert.doesNotThrow(() => sandbox._loadRegisteredView(''));
    assert.equal(sandbox._registeredConsoleView('tenant'), null);
});

test('identity-admin.js registers every fork admin view', () => {
    for (const id of FORK_VIEW_IDS) {
        const re = new RegExp("registerConsoleView\\(\\{ id: '" + id + "',[\\s\\S]*?load:");
        assert.match(identityAdmin, re, `${id} must be registered in identity-admin.js`);
    }
    assert.match(identityAdmin, /typeof window\.registerConsoleView === 'function'/,
        'registration must be guarded so standalone contract tests still load the file');
});

test('audit-console.js owns the 审计日志 and Token 消耗 views', () => {
    for (const id of AUDIT_CONSOLE_VIEW_IDS) {
        const re = new RegExp("registerConsoleView\\(\\{\\s*id: '" + id + "',[\\s\\S]*?load:");
        assert.match(auditConsole, re, `${id} must be registered in audit-console.js`);
    }
    assert.match(auditConsole, /typeof window\.registerConsoleView === 'function'/,
        'registration must be guarded so standalone contract tests still load the file');
});

test('todos.js registers the todo view through the registry', () => {
    assert.match(todos, /registerConsoleView\(\{ id: 'todo', label: 'menu_todo', load: loadTodosView \}\)/);
    assert.match(todos, /typeof window\.registerConsoleView === 'function'/);
});
