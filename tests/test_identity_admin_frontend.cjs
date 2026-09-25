// Run the complete shipped admin script. The DOM fixture only models the
// generated modal controls; browser acceptance owns the visual layout.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/identity-admin.js'), 'utf8');
const consoleSource = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');

// identity-admin.js calls the global `userAvatarHTML` declared by console.js.
// Run the shipped helper block (not a reimplementation) ahead of it so the list
// rows are rendered by the real logic, then the IIFE can see the function.
function consoleAvatarHelpers() {
    const from = consoleSource.indexOf('/* ---- User account avatars');
    const to = consoleSource.indexOf('function _emptyAccount(phase)');
    assert.ok(from >= 0 && to > from, 'missing console.js avatar helper block');
    return consoleSource.slice(from, to);
}

function runAdmin(ctx) {
    vm.runInNewContext(consoleAvatarHelpers() + '\n' + source, ctx, { filename: 'identity-admin.js' });
}

const catalog = ['tenant.info.read', 'tenant.members.read', 'tenant.org.read'];
const role = {
    id: 'role-reviewer', code: 'reviewer', name: 'Organization reviewer',
    version: 7, builtin: false, permissions: ['tenant.info.read', 'tenant.org.read'],
    resource_grants: [
        { resource_kind: 'skill', resource_id: 'custom:knowledge-wiki', action: 'read' },
        { resource_kind: 'model', resource_id: 'provider:deepseek:deepseek-v4-flash', action: 'use' },
    ],
    model_defaults: { chat: 'provider:deepseek:deepseek-v4-flash' },
};

function eventTarget() {
    const listeners = new Map();
    return {
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(fn);
        },
        dispatch(type) {
            for (const fn of listeners.get(type) || []) fn({ target: this, type });
        },
    };
}

function element(tag = 'div') {
    const classes = new Set();
    let html = '';
    const el = {
        ...eventTarget(), tagName: tag.toLowerCase(), id: '', value: '', type: '',
        children: [], style: {}, checked: false, disabled: false, textContent: '',
        get className() { return [...classes].join(' '); },
        set className(value) {
            classes.clear(); String(value).split(/\s+/).filter(Boolean).forEach(c => classes.add(c));
        },
        classList: {
            add: (...names) => names.forEach(c => classes.add(c)),
            remove: (...names) => names.forEach(c => classes.delete(c)),
            contains: name => classes.has(name),
            toggle(name, force) {
                const on = force === undefined ? !classes.has(name) : !!force;
                if (on) classes.add(name); else classes.delete(name);
                return on;
            },
        },
        appendChild(child) { this.children.push(child); child.__parent = this; return child; },
        focus() {},
        closest(selector) {
            let node = this;
            while (node) { if (node.matches && node.matches(selector)) return node; node = node.__parent; }
            return null;
        },
        getAttribute(name) { return this[name] != null ? String(this[name]) : null; },
        matches(selector) {
            if (selector.startsWith('#')) return this.id === selector.slice(1);
            // support ".cls[attr=val]" before the plain ".class" branch
            const clsAttr = selector.match(/^\.([\w-]+)\[([\w-]+)=(?:"|')([^"']*)(?:"|')?\]$/);
            if (clsAttr) return classes.has(clsAttr[1]) && String(this[clsAttr[2]] || '') === clsAttr[3];
            if (selector.startsWith('.')) return classes.has(selector.slice(1));
            if (selector === 'input:checked') return this.tagName === 'input' && this.checked;
            if (selector === 'input[type=checkbox]') return this.tagName === 'input' && this.type === 'checkbox';
            const attrMatch = selector.match(/^\[([\w-]+)=(?:"|')([^"']*)(?:"|')?\]$/);
            if (attrMatch) return String(this[attrMatch[1]] || '') === attrMatch[2];
            return this.tagName === selector;
        },
        querySelectorAll(selector) {
            // Simple recursive match. Only single-part (non-descendant) selectors
            // are needed by the code under test; browser acceptance owns the full
            // CSS engine. Descendant combinators are handled in the test helpers
            // that need them (see resourceToggleIn/resourceListIn).
            const selectors = selector.split(',').map(s => s.trim());
            return this.children.flatMap(child => [
                ...(selectors.some(s => child.matches(s)) ? [child] : []),
                ...child.querySelectorAll(selector),
            ]);
        },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
        get innerHTML() { return html; },
        set innerHTML(value) {
            html = String(value);
            this.children = [];
            // Parse the script's generated elements so checked values and form
            // submission use its actual HTML, not test-authored control state.
            const stack = [this];
            for (const match of html.matchAll(/<\/?([a-z][a-z0-9-]*)\b([^>]*)>/gi)) {
                if (match[0].startsWith('</')) { stack.pop(); continue; }
                const child = element(match[1]);
                for (const attr of match[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
                    const [, name, content = ''] = attr;
                    if (name === 'class') child.className = content;
                    else if (['id', 'value', 'type', 'data-kind', 'data-tab', 'data-cap', 'data-group'].includes(name)) child[name] = content;
                    else if (['checked', 'disabled'].includes(name)) child[name] = true;
                }
                stack[stack.length - 1].appendChild(child);
                if (!['input', 'br', 'hr', 'img', 'meta', 'link'].includes(child.tagName)) stack.push(child);
            }
        },
    };
    return el;
}

function response(data, status = 200) {
    return { ok: status >= 200 && status < 300, status, json: async () => data };
}

function setup(permissionResponse = () => response({ status: 'success', permissions: catalog }), opts = {}) {
    const calls = [];
    const document = { ...eventTarget(), body: element('body'), createElement: element };
    document.getElementById = id => document.body.querySelector('#' + id);
    document.querySelector = sel => document.body.querySelector(sel);
    document.querySelectorAll = sel => document.body.querySelectorAll(sel);
    for (const id of ['role-list', 'role-status', 'role-create-btn']) {
        const el = element(id.endsWith('-btn') ? 'button' : 'div');
        el.id = id;
        document.body.appendChild(el);
    }
    const ctx = {
        document, console,
        sessionStorage: { getItem: () => 'test-tenant' },
        setTimeout() {},
        confirm: () => true,
        escapeHtml: value => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        fetch: async (url, options) => {
            calls.push({ url, options });
            if (opts.onWrite && options && options.method && options.method !== 'GET') {
                const handled = opts.onWrite(url, options);
                if (handled) return handled;
            }
            if (url === '/api/tenant/permissions') return permissionResponse();
            if (url === '/api/tenant/roles') return response({ status: 'success', items: opts.roles || [role] });
            if (url === '/api/tenant/roles/' + role.id) return response({ status: 'success' });
            if (url.startsWith('/api/tenant/authorization/catalog?')) {
                const parsed = new URL(url, 'http://test');
                const kind = parsed.searchParams.get('kind');
                const itembyKind = {
                    skill: [{ resource_id: 'custom:knowledge-wiki', name: 'knowledge-wiki', capability: 'skill' }],
                    model: [{ resource_id: 'provider:deepseek:deepseek-v4-flash', name: 'deepseek-v4-flash', capability: 'model', provider: 'deepseek' }],
                    tool: [{ resource_id: 'builtin.read', name: 'read' }],
                    menu: [{ resource_id: 'nav:chat', name: '对话' }],
                };
                return response({ status: 'success', kind, items: itembyKind[kind] || [], total: (itembyKind[kind] || []).length, page: 1, resource_actions: { skill: ['read', 'use', 'edit', 'enable'], model: ['read', 'use'], tool: ['read', 'execute', 'configure'], menu: ['view'] }[kind] || [] });
            }
            throw Error('Unexpected request: ' + url);
        },
    };
    ctx.window = ctx;
    runAdmin(ctx);
    document.dispatch('DOMContentLoaded');
    return {
        ctx, calls, node: id => document.getElementById(id),
        permissions: () => document.getElementById('adm-fld-permissions')?.querySelectorAll('input[type=checkbox]') || [],
        resourceTab: kind => document.querySelector('.role-editor-tab[data-tab="' + kind + '"]'),
        resourceList: kind => {
            const list = document.getElementById('role-res-list-' + kind);
            return list ? list.querySelectorAll('input[type=checkbox]') : [];
        },
        editorOpen: () => {
            const ed = document.getElementById('role-editor');
            return !!ed && !ed.classList.contains('hidden');
        },
        editorTabs: () => [...(document.querySelectorAll('.role-editor-tab') || [])].map(el => el.getAttribute('data-tab')),
        modalOpen: () => {
            const modal = document.getElementById('admin-modal');
            return !!modal && !modal.classList.contains('hidden');
        },
        catalogRequests: () => calls.filter(call => call.url === '/api/tenant/permissions'),
    };
}

const settle = () => new Promise(resolve => setImmediate(resolve));

async function editRole(h) {
    await h.ctx.loadRolesView();
    h.ctx.adminRowAction('role', 'edit', role.id);
    await settle();
}

test('editing opens the role page editor (not modal) with expected tabs and preserves grants on save', async () => {
    const h = setup();
    await editRole(h);
    assert.equal(h.editorOpen(), true);
    assert.equal(h.modalOpen(), false, 'role edit must not use the shared admin modal');
    assert.deepEqual(h.editorTabs(), ['basic', 'menu', 'skill', 'tool', 'agent', 'model']);
    assert.deepEqual(h.permissions().map(p => p.value), catalog);
    assert.deepEqual(h.permissions().filter(p => p.checked).map(p => p.value), role.permissions);
    assert.equal(h.catalogRequests()[0].options.headers['X-Tenant-ID'], 'test-tenant');
    h.node('role-editor-submit').dispatch('click');
    await settle();
    const saved = h.calls.find(call => call.url === '/api/tenant/roles/' + role.id);
    const body = JSON.parse(saved.options.body);
    assert.equal(body.name, role.name);
    assert.deepEqual(body.permissions, role.permissions);
    assert.equal(body.expected_version, role.version);
    // The unified save (task 3.1) carries the role's resource grants expanded to
    // the kind's allowed actions, plus its model defaults.
    const grants = body.resource_grants;
    assert.deepEqual(grants.filter(g => g.resource_kind === 'skill'), [
        { resource_kind: 'skill', resource_id: 'custom:knowledge-wiki', action: 'read' },
        { resource_kind: 'skill', resource_id: 'custom:knowledge-wiki', action: 'use' },
        { resource_kind: 'skill', resource_id: 'custom:knowledge-wiki', action: 'edit' },
        { resource_kind: 'skill', resource_id: 'custom:knowledge-wiki', action: 'enable' },
    ]);
    assert.deepEqual(grants.filter(g => g.resource_kind === 'model'), [
        { resource_kind: 'model', resource_id: 'provider:deepseek:deepseek-v4-flash', action: 'read' },
        { resource_kind: 'model', resource_id: 'provider:deepseek:deepseek-v4-flash', action: 'use' },
    ]);
    assert.deepEqual(body.model_defaults, { chat: 'provider:deepseek:deepseek-v4-flash' });
    assert.equal(h.editorOpen(), false);
    await editRole(h);
    assert.equal(h.catalogRequests().length, 1, 'successful catalog is reused');
});

const unavailableCatalogs = [
    ['HTTP 500', () => response({ status: 'error', message: 'catalog unavailable' }, 500)],
    ['empty catalog', () => response({ status: 'success', permissions: [] })],
    ['missing catalog', () => response({ status: 'success' })],
    ['non-array catalog', () => response({ status: 'success', permissions: 'tenant.org.read' })],
    ['invalid catalog entries', () => response({ status: 'success', permissions: ['tenant.org.read', null] })],
    ['blank permission', () => response({ status: 'success', permissions: ['   '] })],
    ['invalid JSON', () => ({ ok: true, status: 200, json: async () => { throw Error('Invalid JSON'); } })],
];

for (const [label, unavailable] of unavailableCatalogs) {
    test(`${label} blocks editing and can be retried without retaining an empty catalog`, async () => {
        let ready = false;
        const h = setup(() => ready ? response({ status: 'success', permissions: catalog }) : unavailable());
        await editRole(h);
        assert.equal(h.editorOpen(), false, 'must not expose a saveable role form without permission choices');
        assert.ok(h.node('role-status').textContent, 'show an actionable load error');
        assert.equal(h.node('role-status').classList.contains('opacity-0'), false);
        assert.equal(h.node('role-status').style.color, '#ef4444');
        assert.equal(h.calls.some(call => call.options.method !== 'GET'), false);
        ready = true;
        h.ctx.adminRowAction('role', 'edit', role.id);
        await settle();
        assert.equal(h.editorOpen(), true);
        assert.deepEqual(h.permissions().filter(p => p.checked).map(p => p.value), role.permissions);
        assert.equal(h.catalogRequests().length, 2, 'retry fetches the catalog again');
    });
}

test('new-role button blocks an unavailable catalog and renders choices after retry', async () => {
    let ready = false;
    const h = setup(() => response({ status: 'success', permissions: ready ? catalog : [] }));
    await h.ctx.loadRolesView();
    h.node('role-create-btn').dispatch('click');
    await settle();
    assert.equal(h.editorOpen(), false);
    assert.ok(h.node('role-status').textContent);
    ready = true;
    h.node('role-create-btn').dispatch('click');
    await settle();
    assert.equal(h.editorOpen(), true);
    assert.deepEqual(h.permissions().map(p => p.value), catalog);
    assert.equal(h.permissions().some(p => p.checked), false);
    assert.equal(h.node('role-editor-title').textContent, 'role_create');
    assert.equal(h.catalogRequests().length, 2);
});

test('resource tab preselects existing grants and shows a searchable list', async () => {
    const h = setup();
    await editRole(h);
    assert.equal(h.editorOpen(), true);
    const tab = h.resourceTab('skill');
    assert.ok(tab, 'skill tab exists');
    tab.dispatch('click');
    await settle(); await settle();
    const boxes = h.resourceList('skill');
    assert.ok(boxes.length >= 1, 'skill list renders from the catalog');
    const checked = boxes.filter(b => b.checked).map(b => b.value);
    assert.ok(checked.includes('custom:knowledge-wiki'), 'existing grant is preselected');
});

test('a save drops the model grants the tenant can no longer allocate', async () => {
    // The platform can narrow a tenant's model limit after a role was saved. The
    // dropped model then vanishes from the assignable catalog, so the picker can
    // neither render nor uncheck it — but it kept inflating the model tab badge
    // ("明明只有一个模型，为什么数量是3") and was written straight back on save.
    const stale = 'provider:deepseek:deepseek-v4-pro';
    const drain = async (n) => { for (let i = 0; i < n; i++) await settle(); };
    const h = setup(undefined, { roles: [{
        ...role,
        resource_grants: [
            ...role.resource_grants,
            { resource_kind: 'model', resource_id: stale, action: 'read' },
            { resource_kind: 'model', resource_id: stale, action: 'use' },
        ],
    }] });
    await h.ctx.loadRolesView();
    h.ctx.adminRowAction('role', 'edit', role.id);
    await drain(8);
    assert.equal(h.editorOpen(), true);
    // The catalog offers only the model the tenant still has, so the stale one is
    // unrenderable: the list shows one model and the badge has to agree with it.
    assert.deepEqual(h.resourceList('model').map(b => b.value),
        ['provider:deepseek:deepseek-v4-flash'], 'only the assignable model is listed');
    assert.equal(h.node('role-badge-model').textContent, '1',
        'the badge counts what the picker can show, not the stale grant');
    h.node('role-editor-submit').dispatch('click');
    await drain(4);
    const saved = h.calls.find(c => c.url === '/api/tenant/roles/' + role.id);
    assert.deepEqual(JSON.parse(saved.options.body).resource_grants.filter(g => g.resource_kind === 'model'), [
        { resource_kind: 'model', resource_id: 'provider:deepseek:deepseek-v4-flash', action: 'read' },
        { resource_kind: 'model', resource_id: 'provider:deepseek:deepseek-v4-flash', action: 'use' },
    ], 'the unallocatable model is not written back');
});

test('role create posts the whole draft taken from every tab', async () => {
    const drain = async (n) => { for (let i = 0; i < n; i++) await settle(); };
    const h = setup();
    await h.ctx.loadRolesView();
    h.node('role-create-btn').dispatch('click');
    await drain(3);
    assert.equal(h.editorOpen(), true, 'create opens the editor');
    // basics tab: name, code and a functional permission
    h.node('adm-fld-name').value = 'New reviewer';
    h.node('adm-fld-code').value = 'new-reviewer';
    const memberPerm = h.permissions().find(p => p.value === 'tenant.members.read');
    memberPerm.checked = true;
    memberPerm.dispatch('change');
    // tool tab: add a tool
    h.resourceTab('tool').dispatch('click');
    await drain(3);
    const toolBoxes = h.resourceList('tool');
    assert.ok(toolBoxes.length, 'tool list renders from the catalog');
    toolBoxes[0].checked = true;
    toolBoxes[0].dispatch('change');
    // one click on create must send the merged draft
    h.node('role-editor-submit').dispatch('click');
    await drain(4);
    const writes = h.calls.filter(c =>
        c.options && c.options.method === 'POST' && c.url === '/api/tenant/roles');
    assert.equal(writes.length, 1, 'create issues exactly one POST');
    const body = JSON.parse(writes[0].options.body);
    assert.equal(body.name, 'New reviewer');
    assert.equal(body.code, 'new-reviewer');
    assert.ok(body.permissions.includes('tenant.members.read'), 'basic-tab permission is saved');
    assert.ok(body.resource_grants.some(g => g.resource_kind === 'tool' && g.resource_id === 'builtin.read'), 'tool-tab selection is saved');
});

test('a successful save must not leave the next editor unusable', async () => {
    // The submit button is disabled while a write is in flight. Nothing turns
    // it back on after a success (the editor just closes), so the next create
    // or edit opened in the same page session renders a dead button: the form
    // looks fine, clicks do nothing, and no request is sent.
    const drain = async (n) => { for (let i = 0; i < n; i++) await settle(); };
    const h = setup();
    await h.ctx.loadRolesView();

    h.node('role-create-btn').dispatch('click');
    await drain(3);
    h.node('adm-fld-name').value = 'First role';
    h.node('adm-fld-code').value = 'first-role';
    h.node('role-editor-submit').dispatch('click');
    await drain(4);
    assert.equal(h.editorOpen(), false, 'the first create closes the editor');
    assert.equal(h.node('role-editor-submit').disabled, false,
        'the submit button is re-enabled after a successful write');

    // Second editor opened from the same page: it must be saveable too.
    h.node('role-create-btn').dispatch('click');
    await drain(3);
    assert.equal(h.editorOpen(), true);
    assert.equal(h.node('role-editor-submit').disabled, false,
        'a freshly opened create form starts with an enabled submit button');
    h.node('adm-fld-name').value = 'Second role';
    h.node('adm-fld-code').value = 'second-role';
    h.node('role-editor-submit').dispatch('click');
    await drain(4);
    const writes = h.calls.filter(c =>
        c.options && c.options.method === 'POST' && c.url === '/api/tenant/roles');
    assert.equal(writes.length, 2, 'the second create also posts');
});

test('a successful role edit must not leave the next save unusable', async () => {
    const drain = async (n) => { for (let i = 0; i < n; i++) await settle(); };
    const h = setup();
    await editRole(h);
    h.node('role-editor-submit').dispatch('click');
    await drain(4);
    assert.equal(h.editorOpen(), false, 'the save closes the editor');
    await editRole(h);
    assert.equal(h.node('role-editor-submit').disabled, false,
        'reopening the editor re-enables its save button');
    h.node('role-editor-submit').dispatch('click');
    await drain(4);
    const writes = h.calls.filter(c =>
        c.options && c.options.method === 'POST' && c.url === '/api/tenant/roles/' + role.id);
    assert.equal(writes.length, 2, 'the second save also posts');
});

test('a write that never settles can be recovered by reopening the editor', async () => {
    // A request can hang (server restart, dropped connection). Its in-flight
    // flag would then stick forever, so going back and opening a fresh form has
    // to hand the operator a usable button again.
    const drain = async (n) => { for (let i = 0; i < n; i++) await settle(); };
    const h = setup(undefined, { onWrite: () => new Promise(() => {}) });
    await h.ctx.loadRolesView();
    h.node('role-create-btn').dispatch('click');
    await drain(3);
    h.node('adm-fld-name').value = 'Stuck role';
    h.node('adm-fld-code').value = 'stuck-role';
    h.node('role-editor-submit').dispatch('click');
    await drain(2);
    assert.equal(h.node('role-editor-submit').disabled, true, 'an in-flight write disables the button');
    // Operator gives up on the stuck write, backs out and opens a new form.
    h.node('role-editor-back').dispatch('click');
    await drain(2);
    h.node('role-create-btn').dispatch('click');
    await drain(3);
    assert.equal(h.editorOpen(), true, 'the new form opens');
    assert.equal(h.node('role-editor-submit').disabled, false, 'the reopened form is usable again');
});

test('one save persists edits made across several tabs (unified tabbed save)', async () => {
    // Requirement: clicking the editor's single save must persist every tab,
    // not just the tab that happens to be active. This guards the regression
    // where each tab looked like it needed its own save.
    const drain = async (n) => { for (let i = 0; i < n; i++) await settle(); };
    const h = setup();
    await editRole(h);
    // basics tab: rename + toggle a functional permission
    const nameEl = h.node('adm-fld-name');
    nameEl.value = 'Renamed reviewer';
    nameEl.dispatch('input');
    const memberPerm = h.permissions().find(p => p.value === 'tenant.members.read');
    memberPerm.checked = true;
    memberPerm.dispatch('change');
    // tool tab: add a tool
    h.resourceTab('tool').dispatch('click');
    await drain(3);
    const toolBoxes = h.resourceList('tool');
    assert.ok(toolBoxes.length, 'tool list renders from the catalog');
    toolBoxes[0].checked = true;
    toolBoxes[0].dispatch('change');
    // menu tab: add a menu
    h.resourceTab('menu').dispatch('click');
    await drain(3);
    const menuBoxes = h.resourceList('menu');
    assert.ok(menuBoxes.length, 'menu list renders from the catalog');
    menuBoxes[0].checked = true;
    menuBoxes[0].dispatch('change');
    // a single save
    h.node('role-editor-submit').dispatch('click');
    await drain(4);
    const writes = h.calls.filter(c =>
        c.options && c.options.method === 'POST' && c.url === '/api/tenant/roles/' + role.id);
    assert.equal(writes.length, 1, 'exactly one write request for all tabs');
    const body = JSON.parse(writes[0].options.body);
    assert.equal(body.name, 'Renamed reviewer', 'basic-tab rename is saved');
    assert.ok(body.permissions.includes('tenant.members.read'), 'basic-tab permission is saved');
    assert.ok(body.resource_grants.some(g => g.resource_kind === 'tool' && g.resource_id === 'builtin.read'), 'tool-tab selection is saved');
    assert.ok(body.resource_grants.some(g => g.resource_kind === 'menu' && g.resource_id === 'nav:chat'), 'menu-tab selection is saved');
    assert.ok(body.resource_grants.some(g => g.resource_kind === 'model'), 'an untouched tab keeps its grant');
});

// ---- member ↔ tenant multi-assignment -----------------------------------

const flush = async () => { for (let i = 0; i < 8; i++) await settle(); };

function setupMembers(opts = {}) {
    const calls = [];
    const logs = [];
    // Capture console errors so failure-path cases stay quiet and the recorded
    // evidence can be asserted (see the weak-password case).
    const quietConsole = {
        logs,
        error: (...args) => logs.push(args),
        warn: (...args) => logs.push(args),
        log: () => {}, info: () => {}, debug: () => {},
    };
    const document = { ...eventTarget(), body: element('body'), createElement: element };
    document.getElementById = id => document.body.querySelector('#' + id);
    for (const id of ['member-list', 'member-status', 'member-create-btn', 'member-pagination']) {
        const el = element(id.endsWith('-btn') ? 'button' : 'div');
        el.id = id;
        document.body.appendChild(el);
    }
    const adminTenants = opts.adminTenants || [
        { id: 't1', code: 'acme', name: 'Acme', member: false },
        { id: 't2', code: 'beta', name: 'Beta', member: false },
    ];
    const member = opts.member || {
        id: 'm1', username: 'alice', display_name: 'Alice', active: true,
        role_codes: ['member'], department_id: '', position_text: '', version: 3,
        user_id: 'usr-alice',
    };
    const ctx = {
        document, console: quietConsole,
        sessionStorage: { getItem: () => 't1' },
        setTimeout() {},
        confirm: () => true,
        escapeHtml: value => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        fetch: async (url, options) => {
            calls.push({ url, options });
            if (url === '/api/tenant/roles') return response({ status: 'success', items: [
                { id: 'r-member', code: 'member', name: 'Member', builtin: true },
                { id: 'r-admin', code: 'tenant_admin', name: 'Tenant Admin', builtin: true },
            ] });
            if (url === '/api/tenant/departments') return response({ status: 'success', items: [
                { id: 'd-root', code: '__root__', name: 'Root' },
            ] });
            if (url.startsWith('/api/identity/administered-tenants')) {
                const parsed = new URL(url, 'http://test');
                const target = parsed.searchParams.get('user_id');
                const items = adminTenants.map(t => {
                    const o = { id: t.id, code: t.code, name: t.name };
                    if (target) {
                        o.member = !!t.member;
                        o.member_id = t.member ? (t.member_id || ('mid-' + t.id)) : null;
                        o.member_version = t.member ? (t.member_version || 1) : null;
                    }
                    return o;
                });
                return response({ status: 'success', items });
            }
            if (url.startsWith('/api/tenant/members?')) {
                return response({ status: 'success', items: [member], total: 1 });
            }
            if (url.startsWith('/api/tenant/members/')) return response({ status: 'success' });
            if (url === '/api/tenant/members') {
                if (opts.createMemberResponse && options && options.method === 'POST') {
                    const r = opts.createMemberResponse;
                    return response(r.data, r.status);
                }
                return response({ status: 'success', member: { membership_id: 'mem-new', user_id: 'usr-new' } });
            }
            throw Error('Unexpected request: ' + url);
        },
    };
    ctx.window = ctx;
    runAdmin(ctx);
    document.dispatch('DOMContentLoaded');
    return { ctx, calls, node: id => document.getElementById(id) };
}

function memberPosts(h) {
    return h.calls.filter(c => c.url === '/api/tenant/members' && c.options && c.options.method === 'POST');
}

test('member create issues create-new for the first tenant and bind-existing for the rest', async () => {
    const h = setupMembers();
    h.node('member-create-btn').dispatch('click');
    await flush();
    assert.equal(h.node('admin-modal') && !h.node('admin-modal').classList.contains('hidden'), true);
    // Select the second tenant (t1 is pre-checked as the current tenant).
    const boxes = h.node('adm-fld-tenants').querySelectorAll('input[type=checkbox]');
    const t2 = boxes.find(b => b.value === 't2');
    assert.ok(t2, 'tenant checkbox t2 rendered');
    t2.checked = true;
    t2.dispatch('change');
    h.node('adm-fld-username').value = 'alice';
    h.node('adm-fld-display_name').value = 'Alice';
    h.node('adm-fld-temporary_password').value = 'Str0ngTempPass';
    h.node('admin-modal-submit').dispatch('click');
    await flush();
    const posts = memberPosts(h);
    assert.equal(posts.length, 2);
    const first = JSON.parse(posts[0].options.body);
    const second = JSON.parse(posts[1].options.body);
    assert.equal(first.operation, 'create-new');
    assert.equal(first.temporary_password, 'Str0ngTempPass');
    assert.equal(posts[0].options.headers['X-Tenant-ID'], 't1');
    assert.equal(second.operation, 'bind-existing');
    assert.equal(second.username, 'alice');
    assert.equal(posts[1].options.headers['X-Tenant-ID'], 't2');
});

test('member create surfaces the weak-password reason instead of a generic failure', async () => {
    // Regression: a too-short temporary password made the server answer 500 with
    // an HTML body; apiFetch could not parse it and the modal showed the bare
    // "load-failed". The server now returns a structured weak_password, and the
    // modal must turn that code into the actionable reason.
    const h = setupMembers({
        createMemberResponse: {
            status: 400,
            data: { status: 'error', code: 'weak_password', message: 'weak temporary password' },
        },
    });
    h.node('member-create-btn').dispatch('click');
    await flush();
    h.node('adm-fld-username').value = 'rock';
    h.node('adm-fld-display_name').value = 'Rock';
    h.node('adm-fld-temporary_password').value = '123456';
    h.node('admin-modal-submit').dispatch('click');
    await flush();
    const err = h.node('admin-modal-error');
    assert.equal(err.classList.contains('hidden'), false, 'the reason stays visible in the modal');
    assert.equal(err.textContent, 'tenant_admin_weak_password',
        'the modal shows the actionable reason, not the raw server message or load-failed');
    assert.equal(h.node('admin-modal').classList.contains('hidden'), false,
        'the modal stays open so the input can be corrected');
    const evidence = h.ctx.console.logs.map(args => args.join(' ')).join('\n');
    assert.match(evidence, /http 400 weak_password/,
        'the underlying status and server code are recorded for later diagnosis');
});

test('member tenant edit binds a newly-selected tenant and leaves existing memberships alone', async () => {
    const h = setupMembers({
        adminTenants: [
            { id: 't1', code: 'acme', name: 'Acme', member: true, member_id: 'mid-t1', member_version: 2 },
            { id: 't2', code: 'beta', name: 'Beta', member: false },
        ],
    });
    await h.ctx.loadMembersView();
    await flush();
    h.ctx.adminRowAction('member', 'tenants', 'm1');
    await flush();
    assert.equal(h.node('admin-modal') && !h.node('admin-modal').classList.contains('hidden'), true);
    const boxes = h.node('adm-fld-tenants').querySelectorAll('input[type=checkbox]');
    assert.ok(boxes.find(b => b.value === 't1' && b.checked), 'existing t1 membership preselected');
    assert.ok(boxes.find(b => b.value === 't2' && !b.checked), 't2 not yet a member');
    boxes.find(b => b.value === 't2').checked = true;
    boxes.find(b => b.value === 't2').dispatch('change');
    h.node('admin-modal-submit').dispatch('click');
    await flush();
    const posts = memberPosts(h);
    assert.equal(posts.length, 1, 'only the newly-added tenant is written');
    const added = JSON.parse(posts[0].options.body);
    assert.equal(added.operation, 'bind-existing');
    assert.equal(posts[0].options.headers['X-Tenant-ID'], 't2');
});

test('member tenant edit deactivates a deselected tenant with its expected_version', async () => {
    const h = setupMembers({
        adminTenants: [
            { id: 't1', code: 'acme', name: 'Acme', member: true, member_id: 'mid-t1', member_version: 2 },
            { id: 't2', code: 'beta', name: 'Beta', member: true, member_id: 'mid-t2', member_version: 5 },
        ],
    });
    await h.ctx.loadMembersView();
    await flush();
    h.ctx.adminRowAction('member', 'tenants', 'm1');
    await flush();
    const boxes = h.node('adm-fld-tenants').querySelectorAll('input[type=checkbox]');
    const t2 = boxes.find(b => b.value === 't2');
    t2.checked = false;
    t2.dispatch('change');
    h.node('admin-modal-submit').dispatch('click');
    await flush();
    const posts = h.calls.filter(c =>
        c.url.startsWith('/api/tenant/members/') && c.options && c.options.method === 'POST');
    assert.equal(posts.length, 1, 'only the deselected tenant is deactivated');
    assert.ok(posts[0].url.endsWith('/mid-t2'));
    assert.equal(posts[0].options.headers['X-Tenant-ID'], 't2');
    const body = JSON.parse(posts[0].options.body);
    assert.equal(body.active, false);
    assert.equal(body.expected_version, 5);
});

// ---- list rows render the account face (default or uploaded) --------------
// The tenant member list and the platform account list replaced their generic
// `fa-user` / `fa-user-cog` glyphs with the account's real face: the uploaded
// picture through the read-only by-id route, otherwise the id-stable bundled
// default. The default bytes are static assets, so only the upload hits HTTP.

function setupPlatformUsers(users) {
    const calls = [];
    const document = { ...eventTarget(), body: element('body'), createElement: element };
    document.getElementById = id => document.body.querySelector('#' + id);
    for (const id of ['platform-user-list', 'platform-user-pagination',
        'platform-user-search', 'platform-user-status']) {
        const el = element(id.startsWith('platform-user-s') ? 'input' : 'div');
        el.id = id;
        document.body.appendChild(el);
    }
    const ctx = {
        document, console,
        sessionStorage: { getItem: () => 'tenant-1' },
        setTimeout() {},
        confirm: () => true,
        escapeHtml: value => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        fetch: async url => {
            calls.push({ url });
            if (url === '/auth/me') {
                return response({ status: 'success', user: { is_platform_admin: true } });
            }
            if (url.startsWith('/api/platform/users?')) {
                return response({ status: 'success', items: users, total: users.length, page: 1 });
            }
            throw Error('Unexpected request: ' + url);
        },
    };
    ctx.window = ctx;
    runAdmin(ctx);
    document.dispatch('DOMContentLoaded');
    return { ctx, calls, node: id => document.getElementById(id) };
}

test('platform account rows render the id-stable bundled default, not a glyph', async () => {
    const h = setupPlatformUsers([
        { id: 'usr-alice', username: 'alice', display_name: 'Alice', active: true },
        { id: 'usr-bob', username: 'bob', display_name: 'Bob', active: true, is_platform_admin: true },
    ]);
    await h.ctx.loadPlatformUsersView();
    await flush();
    const html = h.node('platform-user-list').innerHTML;
    assert.match(html, /<img class="user-avatar"/);
    assert.match(html, /src="\/assets\/avatars\/default-3\.svg"/, 'usr-alice -> default-3');
    assert.match(html, /src="\/assets\/avatars\/default-5\.svg"/, 'usr-bob -> default-5');
    assert.doesNotMatch(html, /fa-user-cog/, 'the generic account glyph is gone');
});

test('platform account rows prefer an uploaded picture through the by-id route', async () => {
    const h = setupPlatformUsers([
        { id: 'usr-bob', username: 'bob', display_name: 'Bob', active: true, avatar: 'image' },
    ]);
    await h.ctx.loadPlatformUsersView();
    await flush();
    const html = h.node('platform-user-list').innerHTML;
    assert.match(html, /src="\/api\/users\/usr-bob\/avatar"/);
    // A missing upload file still degrades to the default, never a broken img.
    assert.match(html, /default-5\.svg/);
});

test('tenant member rows render the account face instead of a generic icon', async () => {
    const h = setupMembers({
        member: {
            id: 'm1', username: 'alice', display_name: 'Alice', active: true,
            role_codes: ['member'], department_id: '', position_text: '', version: 3,
            user_id: 'usr-alice',
        },
    });
    await h.ctx.loadMembersView();
    await flush();
    const html = h.node('member-list').innerHTML;
    assert.match(html, /<img class="user-avatar"/);
    assert.match(html, /src="\/assets\/avatars\/default-3\.svg"/);
    assert.doesNotMatch(html, /fa-user\b/, 'the generic member glyph is gone');
});

test('tenant member rows prefer an uploaded picture through the by-id route', async () => {
    const h = setupMembers({
        member: {
            id: 'm1', username: 'alice', display_name: 'Alice', active: true, avatar: 'image',
            role_codes: ['member'], department_id: '', position_text: '', version: 3,
            user_id: 'usr-alice',
        },
    });
    await h.ctx.loadMembersView();
    await flush();
    const html = h.node('member-list').innerHTML;
    assert.match(html, /src="\/api\/users\/usr-alice\/avatar"/);
    assert.match(html, /default-3\.svg/);
});

// ---- built-in role editing ---------------------------------------------

test('built-in roles expose edit but never delete, and lock their code', async () => {
    const h = setup(undefined, { roles: [
        { id: 'r-member', code: 'member', name: '成员', builtin: true, version: 1, permissions: ['chat.use'] },
        { id: 'r-admin', code: 'tenant_admin', name: '租户管理员', builtin: true, version: 1, permissions: ['chat.use'] },
        { id: 'r-custom', code: 'reviewer', name: 'Reviewer', builtin: false, version: 2, permissions: [] },
    ] });
    await h.ctx.loadRolesView();
    const html = h.node('role-list').innerHTML;
    const edits = (html.match(/adminRowAction\('role','edit'/g) || []).length;
    const deletes = (html.match(/adminRowAction\('role','delete'/g) || []).length;
    assert.equal(edits, 3, 'every role, built-in included, offers edit');
    assert.equal(deletes, 1, 'only the custom role offers delete');

    // Editing a built-in opens the real editor with its code locked.
    h.ctx.adminRowAction('role', 'edit', 'r-member');
    await settle();
    await settle();
    assert.equal(h.editorOpen(), true, 'the built-in role editor opens');
    assert.equal(h.node('adm-fld-code').tagName, 'div', 'built-in role code is locked');
});

// ---- organization view: empty state and the create entry point -----------
// Every tenant owns a virtual `__root__` department, so /api/tenant/departments
// is never an empty list. The view therefore has to decide emptiness from the
// *visible* departments, and it has to expose the create control (which ships
// hidden in chat.html) or the operator cannot extend the tree at all.

function setupOrg(items) {
    const calls = [];
    const document = { ...eventTarget(), body: element('body'), createElement: element };
    document.getElementById = id => document.body.querySelector('#' + id);
    for (const [id, tag] of [['org-tree', 'div'], ['org-status', 'div'], ['dept-create-btn', 'button']]) {
        const el = element(tag);
        el.id = id;
        // chat.html ships the create button hidden; mirror that so the test
        // exercises the reveal path instead of a pre-visible control.
        if (id === 'dept-create-btn') el.classList.add('hidden');
        document.body.appendChild(el);
    }
    const ctx = {
        document, console,
        sessionStorage: { getItem: () => 't1' },
        setTimeout() {},
        confirm: () => true,
        escapeHtml: value => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        fetch: async (url, options) => {
            calls.push({ url, options });
            if (url === '/api/tenant/departments') return response({ status: 'success', items });
            throw Error('Unexpected request: ' + url);
        },
    };
    ctx.window = ctx;
    runAdmin(ctx);
    document.dispatch('DOMContentLoaded');
    return { ctx, calls, node: id => document.getElementById(id) };
}

const rootOnly = [{ id: 'd-root', code: '__root__', name: '组织根', parent_id: '', active: 1 }];

test('organization view renders the empty state when only the virtual root exists', async () => {
    const h = setupOrg(rootOnly);
    await h.ctx.loadOrgView();
    const html = h.node('org-tree').innerHTML.trim();
    assert.notEqual(html, '', 'the tree area must never render blank');
    assert.match(html, /org_empty/, 'the empty state is shown when there are no departments');
});

test('organization view exposes the create control so the tree can be maintained', async () => {
    // chat.html ships the button hidden; nothing used to reveal it, so the page
    // offered no way to create a department even for a tenant admin.
    const h = setupOrg(rootOnly);
    assert.equal(h.node('dept-create-btn').classList.contains('hidden'), true,
        'precondition: the create button starts hidden');
    await h.ctx.loadOrgView();
    assert.equal(h.node('dept-create-btn').classList.contains('hidden'), false,
        'loading the org view reveals the create-department button');
});

test('organization view still lists real departments after the empty-state fix', async () => {
    const h = setupOrg([
        { id: 'd-root', code: '__root__', name: '组织根', parent_id: '', active: 1 },
        { id: 'd-eng', code: 'eng', name: '工程部', parent_id: 'd-root', active: 1, sort_order: 1 },
    ]);
    await h.ctx.loadOrgView();
    const html = h.node('org-tree').innerHTML;
    assert.match(html, /工程部/);
    assert.doesNotMatch(html, /org_empty/);
});
