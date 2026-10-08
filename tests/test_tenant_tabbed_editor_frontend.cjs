// Tenant create/edit page: a full-page tabbed editor replacing the old modal.
// The DOM fixture models the generated editor controls; browser acceptance owns
// the visual layout.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/identity-admin.js'), 'utf8');

const acme = {
    id: 'tnt-1', code: 'acme', name: 'Acme', active: true, version: 4,
    space: { id: 'acme', status: 'ready', isolation: 'dedicated-root' },
};

// The console caches the tenant it fetched and mutates that cache after a save,
// so every harness must hand out its own copy — a shared fixture object leaks
// state from one test into the next.
function tenantFixture(over = {}) {
    return { ...acme, space: { ...acme.space }, ...over };
}

function eventTarget() {
    const listeners = new Map();
    return {
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(fn);
        },
        dispatch(type, event = {}) {
            for (const fn of listeners.get(type) || []) fn({ target: this, type, ...event });
        },
    };
}

// A real DOM returns a NodeList from querySelectorAll: index access, length,
// forEach and iteration, but NOT Array.prototype.filter/map. The stub has to be
// equally strict, or production code that reaches for an Array method passes
// here and throws in the browser. That is exactly how the agent picker shipped
// broken: `querySelectorAll(...).filter` threw, the change handler died before
// updating the counter, and Save silently did nothing.
function nodeList(items) {
    const list = {
        length: items.length,
        item: i => (i >= 0 && i < items.length ? items[i] : null),
        forEach: (fn, thisArg) => items.forEach(fn, thisArg),
        [Symbol.iterator]: () => items[Symbol.iterator](),
    };
    items.forEach((item, i) => { list[i] = item; });
    return list;
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
        setAttribute(name, value) { this[name] = String(value); },
        removeAttribute(name) { delete this[name]; },
        matches(selector) {
            if (selector.startsWith('#')) return this.id === selector.slice(1);
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
            const selectors = selector.split(',').map(s => s.trim());
            return nodeList(this.children.flatMap(child => [
                ...(selectors.some(s => child.matches(s)) ? [child] : []),
                ...child.querySelectorAll(selector),
            ]));
        },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
        get innerHTML() { return html; },
        set innerHTML(value) {
            html = String(value);
            this.children = [];
            const stack = [this];
            for (const match of html.matchAll(/<\/?([a-z][a-z0-9-]*)\b([^>]*)>/gi)) {
                if (match[0].startsWith('</')) { stack.pop(); continue; }
                const child = element(match[1]);
                for (const attr of match[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
                    const [, name, content = ''] = attr;
                    if (name === 'class') child.className = content;
                    else if (['id', 'value', 'type', 'data-kind', 'data-tab', 'data-cap',
                              'data-group', 'data-user-id', 'data-user-name', 'data-mode',
                              'data-res-kind', 'autocomplete', 'data-lpignore'].includes(name)) child[name] = content;
                    else if (['checked', 'disabled', 'data-1p-ignore',
                              'readonly'].includes(name)) child[name] = true;
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

const settle = () => new Promise(resolve => setImmediate(resolve));
const flush = async () => { for (let i = 0; i < 10; i++) await settle(); };

function setup(opts = {}) {
    const calls = [];
    // Accounts the admin tab creates. The candidate endpoint must return them
    // afterwards, otherwise "searchable once created" cannot be observed.
    const createdAdmins = [];
    // The tenant's committed admin set the read-only block reads (GET). It is
    // updated on a successful POST so "the display refreshes after save" can be
    // observed rather than assumed.
    let adminItems = (opts.currentAdmins || []).map(a => ({ ...a }));
    let adminReadCount = 0;
    let deferredToggleResolve = null;
    const document = { ...eventTarget(), body: element('body'), createElement: element };
    document.getElementById = id => document.body.querySelector('#' + id);
    document.querySelector = sel => document.body.querySelector(sel);
    document.querySelectorAll = sel => document.body.querySelectorAll(sel);
    for (const id of ['tenant-list', 'tenant-status', 'tenant-create-btn',
                      'tenant-search', 'tenant-empty', 'view-tenant']) {
        const el = element(id === 'tenant-create-btn' ? 'button' : 'div');
        el.id = id;
        if (id === 'tenant-search') el.value = '';
        document.body.appendChild(el);
    }
    const state = { confirm: true };
    // Track storage writes so a test can prove the password is never persisted.
    const storageWrites = [];
    const makeStorage = () => ({
        getItem: () => 't1',
        setItem: (key, value) => { storageWrites.push({ key, value: String(value) }); },
        removeItem: () => {},
    });
    const password = opts.password || 'Str0ngAdminPass';
    const grants = (opts.grants || [
        { resource_kind: 'model', resource_id: 'provider:deepseek:deepseek-v4-flash', action: 'read' },
        { resource_kind: 'model', resource_id: 'provider:deepseek:deepseek-v4-flash', action: 'use' },
    ]).map(g => ({ ...g }));
    const tenant0 = tenantFixture(opts.tenant);
    const catalogs = {
        model: [
            { resource_id: 'provider:deepseek:deepseek-v4-flash', name: 'deepseek-v4-flash', capability: 'model' },
            { resource_id: 'provider:openai:gpt-4o', name: 'gpt-4o', capability: 'model' },
        ],
        tool: [
            { resource_id: 'builtin:web-search', name: 'web-search', capability: 'tool' },
            { resource_id: 'builtin:code-run', name: 'code-run', capability: 'tool' },
        ],
    };
    // Agent tab: the target's own bound agents and the source tenant's copyable
    // candidates. `already_copied` is server-owned provenance, so the mock flips
    // it after a successful copy exactly like the real endpoint would.
    let targetAgents = (opts.targetAgents || []).map(a => ({ ...a }));
    let agentCandidates = (opts.agentCandidates || [
        { source_agent_id: 'alpha', name: 'Alpha', enabled: true, is_default: true,
          already_copied: false, clone_agent_id: null },
        { source_agent_id: 'beta', name: 'Beta', enabled: true, is_default: false,
          already_copied: true, clone_agent_id: 'beta-acme' },
        { source_agent_id: 'gamma', name: 'Gamma', enabled: false, is_default: false,
          already_copied: false, clone_agent_id: null },
    ]).map(c => ({ ...c }));
    function agentReadPayload() {
        const source = opts.copySource === 'none' ? null : {
            tenant_id: 'tnt-default', code: 'default', name: 'Default',
            resolved_by: 'default_agent_binding', default_agent_id: 'alpha',
            candidates: agentCandidates.map(c => ({ ...c })),
        };
        return {
            status: 'success', tenant_id: 'tnt-1',
            agents: targetAgents.map(a => ({ ...a })),
            copy_source: source,
            source_error: source ? null : 'no tenant holds any agent to copy from',
        };
    }
    const ctx = {
        document, console,
        // Translate only the placeholder key so the selected-candidate count is
        // observable; every other key still falls back to itself, which is what
        // the existing HTML assertions rely on.
        I18N: { zh: { tenant_agent_selected_count: '已选 {n} 个' } },
        sessionStorage: makeStorage(),
        localStorage: makeStorage(),
        setTimeout() {},
        confirm: () => state.confirm,
        fetch: async (url, options) => {
            calls.push({ url, options });
            const method = (options && options.method) || 'GET';
            const body = options && options.body ? JSON.parse(options.body) : null;
            if (url === '/auth/me') {
                return response({ status: 'success', user: { id: 'u-root', username: 'root', is_platform_admin: true } });
            }
            if (url === '/api/platform/tenants' && method === 'GET') {
                return response({ status: 'success', items: [tenant0] });
            }
            if (url === '/api/platform/tenants' && method === 'POST') {
                if (body.recent_password !== password) {
                    return response({ status: 'error', code: 'invalid_old',
                                      message: 'recent password required' }, 401);
                }
                return response({ status: 'success', tenant: { ...tenant0, id: 'tnt-new', code: 'newco', name: 'NewCo', version: 1 } });
            }
            if (url === '/api/platform/tenants/tnt-new' && method === 'GET') {
                return response({ status: 'success', tenant: { ...tenant0, id: 'tnt-new', code: 'newco', name: 'NewCo', version: 1 } });
            }
            if (url === '/api/platform/tenants/tnt-new/resources') {
                return response({ status: 'success', grants: [] });
            }
            if (url === '/api/platform/tenants/tnt-1' && method === 'GET') {
                return response({ status: 'success', tenant: tenant0 });
            }
            if (url === '/api/platform/tenants/tnt-1' && method === 'POST') {
                if (opts.profileStatus && opts.profileStatus !== 200) {
                    return response({ status: 'error', code: 'conflict' }, opts.profileStatus);
                }
                if (body.recent_password !== password) {
                    return response({ status: 'error', code: 'invalid_old',
                                      message: 'recent password required' }, 401);
                }
                return response({ status: 'success', tenant: {
                    id: 'tnt-1', code: tenant0.code, name: body.name,
                    active: body.active, version: tenant0.version + 1,
                } });
            }
            if (url === '/api/platform/tenants/tnt-1/resources') {
                if (method === 'PUT') {
                    if (opts.grantStatus && opts.grantStatus !== 200) {
                        return response({ status: 'error', code: 'conflict' }, opts.grantStatus);
                    }
                    return response({ status: 'success', grants: body.grants });
                }
                return response({ status: 'success', grants: grants });
            }
            if (url.startsWith('/api/platform/tenants/tnt-1/authorization/catalog')) {
                const parsed = new URL(url, 'http://test');
                const kind = parsed.searchParams.get('kind');
                const q = (parsed.searchParams.get('q') || '').toLowerCase();
                // Mirror the real endpoint, which filters by name/provider: a
                // term that matches nothing must yield an empty page, not the
                // whole catalog, or a search bug cannot be reproduced here.
                const all = catalogs[kind] || [];
                const items = q ? all.filter(i => i.name.toLowerCase().includes(q)) : all;
                return response({ status: 'success', kind, items, total: items.length, page: 1 });
            }
            if (url === '/api/platform/tenants/tnt-1/admins' && method === 'GET') {
                adminReadCount += 1;
                if (opts.adminReadStatus && opts.adminReadStatus !== 200) {
                    return response({ status: 'error', code: 'server_error',
                                      message: 'boom' }, opts.adminReadStatus);
                }
                // A card refresh happens after a successful account write; making
                // only that second read fail proves the one-time result survives.
                if (opts.adminReadFailAfterFirst && adminReadCount > 1) {
                    return response({ status: 'error', code: 'server_error',
                                      message: 'boom' }, 500);
                }
                return response({ status: 'success', items: adminItems.map(a => ({ ...a })) });
            }
            if (url === '/api/platform/tenants/tnt-1/admins') {
                if (opts.adminStatus && opts.adminStatus !== 200) {
                    return response({ status: 'error', code: 'conflict' }, opts.adminStatus);
                }
                if (body && body.recent_password !== password) {
                    return response({ status: 'error', code: 'invalid_old',
                                      message: 'recent password required' }, 401);
                }
                if (body && body.mode === 'new') {
                    // Mirror the service's real rejections so the frontend is
                    // tested against the codes the server actually sends.
                    if (opts.adminError) {
                        return response({ status: 'error', code: opts.adminError.code,
                                          message: opts.adminError.message },
                                        opts.adminError.status);
                    }
                    // A duplicate username is the only 409 this endpoint raises.
                    if (opts.usernameTaken) {
                        return response({ status: 'error', code: 'conflict',
                                          message: 'username already exists' }, 409);
                    }
                    createdAdmins.push({
                        id: 'usr-' + body.username, username: body.username,
                        display_name: body.display_name, active: true,
                    });
                }
                // The write just changed who administers the tenant; the mock
                // reflects it so "the read-only block refreshes after save" is
                // observed rather than assumed.
                adminItems = (body && body.mode === 'new')
                    ? [{ membership_id: 'mem-1', user_id: 'usr-' + body.username,
                         username: body.username, display_name: body.display_name }]
                    : [{ membership_id: 'mem-1', user_id: body.user_id,
                         username: body.user_id === 'usr-root' ? 'root'
                             : String(body.user_id || '').replace(/^usr-/, ''),
                         display_name: body.display_name || 'Root' }];
                return response({ status: 'success', membership: { membership_id: 'mem-1' } });
            }
            if (url === '/api/platform/tenants/tnt-1/agents' && method === 'GET') {
                if (opts.agentReadStatus && opts.agentReadStatus !== 200) {
                    return response({ status: 'error', code: 'server_error',
                                      message: 'boom' }, opts.agentReadStatus);
                }
                return response(agentReadPayload());
            }
            if (url === '/api/platform/tenants/tnt-1/agents' && method === 'POST') {
                if (opts.agentCopyStatus && opts.agentCopyStatus !== 200) {
                    return response({ status: 'error', code: 'conflict',
                                      message: 'boom' }, opts.agentCopyStatus);
                }
                if (!body || body.action !== 'copy') {
                    return response({ status: 'error', code: 'invalid_request' }, 400);
                }
                if (body.recent_password !== password) {
                    return response({ status: 'error', code: 'invalid_old',
                                      message: 'recent password required' }, 401);
                }
                const picked = body.source_agent_ids || [];
                if (!picked.length) {
                    return response({ status: 'error', code: 'bad_request' }, 400);
                }
                const wasEmpty = targetAgents.length === 0;
                const copied = [];
                const skipped = [];
                const failed = [];
                for (const id of picked) {
                    const cand = agentCandidates.find(c => c.source_agent_id === id);
                    if (!cand) {
                        failed.push({ source_agent_id: id, error: 'not copyable' });
                        continue;
                    }
                    if (cand.already_copied) {
                        skipped.push({ source_agent_id: id, agent_id: cand.clone_agent_id });
                        continue;
                    }
                    if (opts.agentCopyFails && opts.agentCopyFails.includes(id)) {
                        failed.push({ source_agent_id: id, error: 'workspace unavailable' });
                        continue;
                    }
                    const newId = id + '-acme';
                    cand.already_copied = true;
                    cand.clone_agent_id = newId;
                    targetAgents.push({
                        id: newId, name: cand.name, enabled: cand.enabled, is_default: false,
                    });
                    copied.push({ source_agent_id: id, agent_id: newId });
                }
                let defaultAgentId = null;
                if (wasEmpty && copied.length) {
                    const preferred = copied.find(c => c.source_agent_id === 'alpha') || copied[0];
                    defaultAgentId = preferred.agent_id;
                    const row = targetAgents.find(a => a.id === defaultAgentId);
                    if (row) row.is_default = true;
                }
                return response({
                    status: 'success', selected: picked.length,
                    copied: copied.length, skipped: skipped.length, failed: failed,
                    copied_agent_ids: copied, skipped_agent_ids: skipped,
                    default_agent_id: defaultAgentId,
                });
            }
            if (/^\/api\/platform\/users\/[^/]+$/.test(url) && method === 'PATCH') {
                if (opts.toggleStatus && opts.toggleStatus !== 200) {
                    return response({ status: 'error', code: opts.toggleCode || 'conflict',
                                      message: opts.toggleMessage || 'toggle failed' },
                                    opts.toggleStatus);
                }
                if (body.recent_password !== password) {
                    return response({ status: 'error', code: 'invalid_old',
                                      message: 'recent password required' }, 401);
                }
                const uid = decodeURIComponent(url.split('/').pop());
                const apply = () => {
                    adminItems = adminItems.map(a => a.user_id === uid
                        ? { ...a, account_active: body.active,
                            account_version: (a.account_version || 0) + 1,
                            is_platform_admin: body.is_platform_admin }
                        : a);
                    return response({ status: 'success', user: { id: uid, active: body.active } });
                };
                if (opts.deferToggle) {
                    // Hold the response so a test can change the editing target
                    // before this "late" reply lands.
                    return await new Promise(resolve => {
                        deferredToggleResolve = () => resolve(apply());
                    });
                }
                return apply();
            }
            if (/^\/api\/platform\/users\/[^/]+\/password$/.test(url) && method === 'POST') {
                if (opts.resetStatus && opts.resetStatus !== 200) {
                    return response({ status: 'error', code: opts.resetCode || 'conflict',
                                      message: opts.resetMessage || 'reset failed' },
                                    opts.resetStatus);
                }
                if (body.recent_password !== password) {
                    return response({ status: 'error', code: 'invalid_old',
                                      message: 'recent password required' }, 401);
                }
                return response({ status: 'success', temporary_password: 'Tmp-Pass-123',
                                  must_change_password: true });
            }
            if (url.startsWith('/api/platform/users')) {
                return response({ status: 'success', items: [
                    { id: 'usr-root', username: 'root', display_name: 'Root', active: true },
                ].concat(createdAdmins), total: 1 + createdAdmins.length, page: 1 });
            }
            throw Error('Unexpected request: ' + method + ' ' + url);
        },
    };
    ctx.window = ctx;
    vm.runInNewContext(source, ctx, { filename: 'identity-admin.js' });
    document.dispatch('DOMContentLoaded');
    return {
        ctx, calls, state, storageWrites,
        deferToggle: () => { if (deferredToggleResolve) deferredToggleResolve(); },
        node: id => document.getElementById(id),
        passwordModalOpen: () => {
            const el = document.getElementById('tenant-password-modal');
            return !!el && !el.classList.contains('hidden');
        },
        editorOpen: () => {
            const ed = document.getElementById('tenant-editor');
            return !!ed && !ed.classList.contains('hidden');
        },
        editorTabs: () => [...(document.querySelectorAll('.tenant-editor-tab') || [])]
            .map(el => el.getAttribute('data-tab')),
        activeTab: () => {
            // The DOM stub matches single classes only, so filter explicitly
            // instead of relying on a ".x.active" compound selector.
            const tabs = [...(document.querySelectorAll('.tenant-editor-tab') || [])];
            const el = tabs.find(t => t.classList.contains('active'));
            return el ? el.getAttribute('data-tab') : null;
        },
        activePanel: () => {
            const panels = [...(document.querySelectorAll('.tenant-editor-panel') || [])];
            const el = panels.find(p => p.classList.contains('active'));
            return el ? el.id : null;
        },
        modalOpen: () => {
            const modal = document.getElementById('admin-modal');
            return !!modal && !modal.classList.contains('hidden');
        },
    };
}

async function openEditor(h, id = 'tnt-1') {
    await h.ctx.loadTenantView();
    await flush();
    h.ctx.adminRowAction('tenant', 'edit', id);
    await flush();
}

// ---- 4.1 editor skeleton --------------------------------------------------

test('editing a tenant opens the full-page tabbed editor, not the modal', async () => {
    const h = setup();
    await openEditor(h);
    assert.equal(h.editorOpen(), true);
    assert.equal(h.modalOpen(), false, 'tenant edit must not use the shared admin modal');
    assert.deepEqual(h.editorTabs(), ['basic', 'model', 'tool', 'agent', 'admin']);
    assert.equal(h.activeTab(), 'basic', 'basic information is the default tab');
    assert.equal(h.activePanel(), 'tenant-panel-basic');
    // The top bar identifies the tenant being edited.
    assert.equal(h.node('tenant-editor-title').textContent, 'tenant_edit_title');
    assert.equal(h.node('tenant-editor-sub').textContent, 'acme');
});

test('the new-tenant button opens the same editor in create mode', async () => {
    const h = setup();
    await h.ctx.loadTenantView();
    await flush();
    h.node('tenant-create-btn').dispatch('click');
    await flush();
    assert.equal(h.editorOpen(), true);
    assert.equal(h.modalOpen(), false);
    assert.deepEqual(h.editorTabs(), ['basic', 'model', 'tool', 'agent', 'admin']);
    assert.equal(h.node('tenant-editor-title').textContent, 'tenant_create');
});

test('tenant rows no longer offer a separate admin button', async () => {
    const h = setup();
    await h.ctx.loadTenantView();
    await flush();
    const html = h.node('tenant-list').innerHTML;
    assert.ok(html.includes("'tenant','edit'"), 'edit row action stays');
    assert.equal(html.includes("'tenant','admin'"), false,
        'admin configuration moved into the editor tab');
});

// ---- 4.2 tab switching keeps the draft and warns before leaving -----------

test('switching tabs keeps uncommitted input and flags the draft unsaved', async () => {
    const h = setup();
    await openEditor(h);
    h.node('tenant-fld-name').value = 'Acme Renamed';
    h.node('tenant-fld-name').dispatch('input');
    await flush();
    h.node('tenant-editor-tab-model').dispatch('click');
    await flush();
    assert.equal(h.activeTab(), 'model', 'tab switch happened');
    assert.equal(h.node('tenant-fld-name').value, 'Acme Renamed', 'draft input is retained');
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), true,
        'an unsaved draft is flagged');

    h.node('tenant-editor-tab-basic').dispatch('click');
    await flush();
    assert.equal(h.activeTab(), 'basic');
    assert.equal(h.node('tenant-fld-name').value, 'Acme Renamed');
});

test('leaving the editor with an unsaved draft asks before discarding', async () => {
    const h = setup();
    await openEditor(h);
    h.node('tenant-fld-name').value = 'Acme Renamed';
    h.node('tenant-fld-name').dispatch('input');
    await flush();
    h.state.confirm = false;
    assert.equal(h.ctx.__identityAdminDirtyGuard__(), false, 'declined discard keeps the editor open');
    assert.equal(h.editorOpen(), true);
    h.state.confirm = true;
    assert.equal(h.ctx.__identityAdminDirtyGuard__(), true, 'accepted discard allows navigation');
    assert.equal(h.editorOpen(), false);
});

test('a clean editor leaves without prompting', async () => {
    const h = setup();
    await openEditor(h);
    h.state.confirm = false;
    assert.equal(h.ctx.__identityAdminDirtyGuard__(), true, 'no draft means no prompt');
});

// ---- 5.x basic information tab -------------------------------------------

function profilePosts(h) {
    return h.calls.filter(c => c.url === '/api/platform/tenants/tnt-1' &&
        c.options && c.options.method === 'POST');
}

test('saving basic information posts the profile operation with the enabled flag', async () => {
    const h = setup();
    await openEditor(h);
    h.node('tenant-fld-name').value = 'Acme Renamed';
    h.node('tenant-fld-name').dispatch('input');
    h.node('tenant-fld-active').checked = false;
    h.node('tenant-fld-active').dispatch('change');
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    const posts = profilePosts(h);
    assert.equal(posts.length, 1, 'exactly one profile write');
    const body = JSON.parse(posts[0].options.body);
    assert.equal(body.operation, 'profile');
    assert.equal(body.name, 'Acme Renamed');
    assert.equal(body.active, false, 'the enabled toggle really reaches the request body');
    assert.equal(body.expected_version, 4, 'the loaded version guards the write');
    assert.equal(body.recent_password, 'Str0ngAdminPass');
});

test('the top bar refreshes its enabled badge and version after a save', async () => {
    const h = setup();
    await openEditor(h);
    assert.equal(h.node('tenant-editor-active-badge').textContent, 'active');
    assert.equal(h.node('tenant-editor-version').textContent, 'tenant_version_label 4');
    h.node('tenant-fld-active').checked = false;
    h.node('tenant-fld-active').dispatch('change');
    h.node('tenant-fld-name').value = 'Acme Renamed';
    h.node('tenant-fld-name').dispatch('input');
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);
    assert.equal(h.node('tenant-editor-active-badge').textContent, 'inactive');
    assert.equal(h.node('tenant-editor-version').textContent, 'tenant_version_label 5');
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), false,
        'a committed draft is no longer flagged unsaved');
});

test('a version conflict keeps the draft and never reports success', async () => {
    const h = setup({ profileStatus: 409 });
    await openEditor(h);
    h.node('tenant-fld-name').value = 'Acme Renamed';
    h.node('tenant-fld-name').dispatch('input');
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    const err = h.node('tenant-editor-error');
    assert.ok(err.textContent.includes('tenant_editor_conflict'),
        'the conflict is surfaced with its reload guidance');
    assert.ok(err.textContent.includes('tenant_tab_basic'),
        'and the batch names the step that failed');
    assert.equal(err.classList.contains('hidden'), false, 'the conflict is surfaced');
    assert.equal(h.node('tenant-fld-name').value, 'Acme Renamed', 'the draft is retained');
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), true,
        'the unsaved draft is still flagged');
    assert.equal(h.node('tenant-status').textContent.includes('admin_saved'), false,
        'success is never claimed for a rejected write');
});

test('create mode can edit the code and disables the other tabs', async () => {
    const h = setup();
    await h.ctx.loadTenantView();
    await flush();
    h.node('tenant-create-btn').dispatch('click');
    await flush();

    assert.equal(h.node('tenant-fld-code').readOnly, false, 'the code is enterable on create');
    ['model', 'tool', 'agent', 'admin'].forEach(tab => {
        assert.equal(h.node('tenant-editor-tab-' + tab).disabled, true,
            tab + ' cannot be configured before the tenant exists');
    });
    // No write is issued just by opening create mode.
    assert.equal(h.calls.some(c => c.options && ['POST', 'PUT'].includes(c.options.method)), false);
});

test('create mode posts code/name/recent_password and stays on the editor', async () => {
    const h = setup();
    await h.ctx.loadTenantView();
    await flush();
    h.node('tenant-create-btn').dispatch('click');
    await flush();
    h.node('tenant-fld-code').value = 'newco';
    h.node('tenant-fld-code').dispatch('input');
    h.node('tenant-fld-name').value = 'NewCo';
    h.node('tenant-fld-name').dispatch('input');
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    const posts = h.calls.filter(c => c.url === '/api/platform/tenants' &&
        c.options && c.options.method === 'POST');
    assert.equal(posts.length, 1);
    const body = JSON.parse(posts[0].options.body);
    assert.deepEqual(Object.keys(body).sort(), ['code', 'name', 'recent_password']);
    assert.equal(h.editorOpen(), true, 'the editor stays open for the follow-up tabs');
    assert.equal(h.activeTab(), 'basic', 'it lands on basic information');
    assert.equal(h.node('tenant-editor-foot-hint').textContent, 'tenant_editor_created_hint',
        'the operator is pointed at the tenant management tab');
});

// ---- 6.x model / tool authorization tabs ---------------------------------

function grantPuts(h) {
    return h.calls.filter(c => c.url === '/api/platform/tenants/tnt-1/resources' &&
        c.options && c.options.method === 'PUT');
}

function grantBoxes(h, kind) {
    const host = h.node('tenant-grant-' + kind);
    const list = host && host.querySelector('.resource-kind-list');
    // querySelectorAll yields a NodeList, so spread before using Array methods.
    return list ? [...list.querySelectorAll('input[type=checkbox]')] : [];
}

test('the model tab loads the model catalog and grants read+use', async () => {
    const h = setup();
    await openEditor(h);
    h.node('tenant-editor-tab-model').dispatch('click');
    await flush();

    const catalogs = h.calls.filter(c =>
        c.url.startsWith('/api/platform/tenants/tnt-1/authorization/catalog'));
    assert.equal(catalogs.length, 1, 'the model catalog is fetched once');
    assert.ok(catalogs[0].url.includes('kind=model'));
    const boxes = grantBoxes(h, 'model');
    assert.equal(boxes.length, 2, 'the whole platform model catalog is offered');
    assert.equal(boxes.filter(b => b.checked).length, 1, 'existing model limits are preselected');
    // Re-asserting the current selection flags the tab dirty; the payload below
    // then proves the preselected read+use set is what would be committed.
    const picked = boxes.find(b => b.checked);
    picked.checked = true;
    picked.dispatch('change');
    await flush();

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    const puts = grantPuts(h);
    assert.equal(puts.length, 1);
    const body = JSON.parse(puts[0].options.body);
    const model = body.grants.filter(g => g.resource_kind === 'model');
    assert.deepEqual(model.map(g => `${g.resource_id}|${g.action}`).sort(), [
        'provider:deepseek:deepseek-v4-flash|read',
        'provider:deepseek:deepseek-v4-flash|use',
    ]);
    assert.equal(body.expected_version, 4);
});

test('the tool tab loads the tool catalog and grants read+execute+configure', async () => {
    const h = setup();
    await openEditor(h);
    h.node('tenant-editor-tab-tool').dispatch('click');
    await flush();

    const catalogs = h.calls.filter(c =>
        c.url.startsWith('/api/platform/tenants/tnt-1/authorization/catalog'));
    assert.equal(catalogs.length, 1);
    assert.ok(catalogs[0].url.includes('kind=tool'));
    const boxes = grantBoxes(h, 'tool');
    assert.equal(boxes.length, 2);
    boxes[0].checked = true;
    boxes[0].dispatch('change');
    await flush();

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    const body = JSON.parse(grantPuts(h)[0].options.body);
    const tool = body.grants.filter(g => g.resource_kind === 'tool');
    assert.deepEqual(tool.map(g => g.action).sort(), ['configure', 'execute', 'read'],
        'a granted tool keeps its full action set');
});

function grantSearch(h, kind) {
    return h.node('tenant-grant-' + kind).querySelector('.resource-kind-search');
}

function grantListHtml(h, kind) {
    const list = h.node('tenant-grant-' + kind).querySelector('.resource-kind-list');
    return list ? list.innerHTML : '';
}

// The model/tool tabs sit in a panel that also renders password inputs, so a
// browser password manager can autofill this non-credential search box. A stray
// term there filters the whole catalog away, which is exactly how the tab came
// to look empty with the models still granted.
test('the grant search box is not autofillable by a password manager', async () => {
    const h = setup();
    await openEditor(h);
    h.node('tenant-editor-tab-model').dispatch('click');
    await flush();

    const search = grantSearch(h, 'model');
    assert.equal(String(search.autocomplete), 'off');
    assert.equal(search.getAttribute('data-lpignore'), 'true');
    assert.ok(search.getAttribute('data-1p-ignore') != null, '1Password opt-out is present');
    // A readonly field is not autofilled, and it is released on first contact so
    // the operator can still type.
    assert.equal(search.getAttribute('readonly'), 'readonly');
    search.dispatch('focus');
    assert.equal(search.getAttribute('readonly'), null, 'focus releases the field');
});

test('a search that matches nothing says so instead of "no resources selected"', async () => {
    const h = setup();
    await openEditor(h);
    h.node('tenant-editor-tab-model').dispatch('click');
    await flush();

    const search = grantSearch(h, 'model');
    search.value = 'admin';
    search.dispatch('keyup', { key: 'Enter' });
    await flush();

    assert.deepEqual(grantBoxes(h, 'model'), [], 'no model matches "admin"');
    assert.match(grantListHtml(h, 'model'), /admin_resources_no_match/,
        'the empty state names the filter, so the cause is visible');
    assert.ok(!/admin_resources_none/.test(grantListHtml(h, 'model')),
        'it must not claim nothing is selected while a filter hides the catalog');
});

test('clearing the picker also clears the search, so the catalog comes back', async () => {
    const h = setup();
    await openEditor(h);
    h.node('tenant-editor-tab-model').dispatch('click');
    await flush();

    const search = grantSearch(h, 'model');
    search.value = 'gpt';
    search.dispatch('keyup', { key: 'Enter' });
    await flush();
    assert.equal(grantBoxes(h, 'model').length, 1, 'the search does narrow the list');

    search.value = 'admin';
    search.dispatch('keyup', { key: 'Enter' });
    await flush();
    assert.equal(grantBoxes(h, 'model').length, 0);

    // "清空" used to deselect only, so the term stayed and the list stayed
    // empty: the button looked broken and the models stayed invisible.
    h.node('tenant-grant-model').querySelector('.resource-kind-clear').dispatch('click');
    await flush();
    assert.equal(search.value, '', 'the search term is cleared too');
    assert.equal(grantBoxes(h, 'model').length, 2, 'the whole catalog is visible again');
});

test('grant candidate sets come from the platform catalog, not the tenant limits', async () => {
    const h = setup();
    await openEditor(h);
    h.node('tenant-editor-tab-model').dispatch('click');
    await flush();
    // `provider:openai:gpt-4o` is not currently granted to this tenant, so seeing
    // it proves the candidates are not filtered by the existing allocation.
    const values = grantBoxes(h, 'model').map(b => b.value);
    assert.ok(values.includes('provider:openai:gpt-4o'),
        'the candidate set is the full platform catalog');
});

test('the model and tool tabs save independently without clobbering each other', async () => {
    const h = setup();
    await openEditor(h);
    // Model tab: additionally grant gpt-4o.
    h.node('tenant-editor-tab-model').dispatch('click');
    await flush();
    const gpt = grantBoxes(h, 'model').find(b => b.value === 'provider:openai:gpt-4o');
    gpt.checked = true;
    gpt.dispatch('change');
    await flush();
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    // Tool tab: grant web-search.
    h.node('tenant-editor-tab-tool').dispatch('click');
    await flush();
    const tool = grantBoxes(h, 'tool').find(b => b.value === 'builtin:web-search');
    tool.checked = true;
    tool.dispatch('change');
    await flush();
    h.node('tenant-editor-submit').dispatch('click');
    await flush();

    const puts = grantPuts(h);
    assert.equal(puts.length, 2, 'each tab commits its own change');
    const first = JSON.parse(puts[0].options.body);
    assert.equal(first.expected_version, 4);
    const second = JSON.parse(puts[1].options.body);
    assert.equal(second.expected_version, 5, 'the second save uses the bumped version');
    // The tool save must still carry the model limits chosen a moment ago.
    const secondModel = second.grants.filter(g => g.resource_kind === 'model');
    assert.equal(secondModel.some(g => g.resource_id === 'provider:openai:gpt-4o'),
        true, 'the other kind is not dropped by the wholesale PUT');
    assert.equal(second.grants.some(g => g.resource_kind === 'tool' && g.resource_id === 'builtin:web-search'),
        true);
});

test('a conflicted grant save keeps the checked draft', async () => {
    const h = setup({ grantStatus: 409 });
    await openEditor(h);
    h.node('tenant-editor-tab-model').dispatch('click');
    await flush();
    const gpt = grantBoxes(h, 'model').find(b => b.value === 'provider:openai:gpt-4o');
    gpt.checked = true;
    gpt.dispatch('change');
    await flush();
    h.node('tenant-editor-submit').dispatch('click');
    await flush();

    assert.ok(h.node('tenant-editor-error').textContent.includes('tenant_editor_conflict'),
        'the conflict is reported');
    assert.equal(grantBoxes(h, 'model').find(b => b.value === 'provider:openai:gpt-4o').checked, true,
        'the checks survive the rejected save');
});

// ---- 7.x tenant management tab (space + admin designation) ----------------

async function openAdminTab(h) {
    await openEditor(h);
    h.node('tenant-editor-tab-admin').dispatch('click');
    await flush();
}

function adminPosts(h) {
    // The same URL also serves the read-only GET; only writes count as posts.
    return h.calls.filter(c => c.url === '/api/platform/tenants/tnt-1/admins'
        && c.options && c.options.method === 'POST');
}

test('the admin tab lists platform accounts and posts the picked stable id', async () => {
    const h = setup();
    await openAdminTab(h);
    const option = h.ctx.document.querySelector('.user-picker-option');
    assert.ok(option, 'the tenant admin picker lists candidate accounts');
    option.dispatch('click');
    await flush();
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    assert.equal(adminPosts(h).length, 1, 'the picked account is submitted');
    const body = JSON.parse(adminPosts(h)[0].options.body);
    assert.equal(body.user_id, 'usr-root',
        'the stable account id is sent, never the display name');
});

test('the admin tab refuses to submit while no account is picked', async () => {
    const h = setup();
    await openAdminTab(h);
    // The operator has edited the tab (so it is part of the batch) but has not
    // chosen an account yet: that must never post an empty target.
    h.node('tenant-fld-admin_display').value = 'Someone';
    h.node('tenant-fld-admin_display').dispatch('input');
    h.node('tenant-editor-submit').dispatch('click');
    await flush();

    assert.equal(adminPosts(h).length, 0, 'no write without a target account');
    assert.equal(h.passwordModalOpen(), false,
        'an incomplete form is rejected before asking for a password');
    assert.equal(h.node('tenant-editor-error').textContent, 'admin_user_picker_required',
        'the error names the missing selection rather than a generic required-field message');
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), true,
        'the tab stays dirty so the admin is still owed');
});

test('the admin tab renders the tenant space read-only and never a host path', async () => {
    const h = setup({
        tenant: {
            space: {
                id: 'acme', status: 'ready', isolation: 'dedicated-root',
                shared_root: '/srv/tenants/acme',
            },
        },
    });
    await openAdminTab(h);

    const html = h.node('tenant-space-card').innerHTML;
    assert.ok(html.includes('acme'), 'the space id is shown');
    assert.ok(html.includes('tenant_space_ready'), 'the ready state is shown');
    assert.ok(html.includes('dedicated-root'), 'the isolation kind is shown');
    assert.ok(!html.includes('shared_root'), 'the raw host field is never rendered');
    assert.ok(!html.includes('/srv/tenants'), 'no host path is rendered');
});

test('a space that is not ready is labelled as such', async () => {
    const h = setup({ tenant: { space: { id: 'acme', status: 'missing', isolation: 'dedicated-root' } } });
    await openAdminTab(h);
    const html = h.node('tenant-space-card').innerHTML;
    assert.ok(html.includes('tenant_space_not_ready'));
    assert.ok(!html.includes('tenant_space_ready'), 'the ready label is not used for a missing root');
});

test('a tenant without a space projection says so instead of guessing', async () => {
    const h = setup({ tenant: { space: null } });
    await openAdminTab(h);
    assert.equal(h.node('tenant-space-card').innerHTML.includes('tenant_space_unavailable'), true);
});

// ---- 8.x one save for every dirty tab + unified password prompt ----------

function writeCalls(h) {
    return h.calls.filter(c => c.options && ['POST', 'PUT'].includes(c.options.method));
}
const bodyOf = call => JSON.parse(call.options.body);

async function dirtyAdmin(h) {
    await openAdminTab(h);
    const option = h.ctx.document.querySelector('.user-picker-option');
    option.dispatch('click');
    await flush();
}

async function dirtyBasics(h) {
    h.node('tenant-editor-tab-basic').dispatch('click');
    await flush();
    h.node('tenant-fld-name').value = 'Acme Renamed';
    h.node('tenant-fld-name').dispatch('input');
}

async function dirtyModel(h) {
    h.node('tenant-editor-tab-model').dispatch('click');
    await flush();
    const gpt = grantBoxes(h, 'model').find(b => b.value === 'provider:openai:gpt-4o');
    gpt.checked = true;
    gpt.dispatch('change');
    await flush();
}

async function dirtyTool(h) {
    h.node('tenant-editor-tab-tool').dispatch('click');
    await flush();
    const box = grantBoxes(h, 'tool')[0];
    box.checked = true;
    box.dispatch('change');
    await flush();
}

// A test only types the right password on the first try when it says so; the
// wrong-password test drives the retry path explicitly.
async function confirmPassword(h, value = 'Str0ngAdminPass') {
    h.node('tenant-password-input').value = value;
    h.node('tenant-password-confirm').dispatch('click');
    await flush();
}

test('one save commits every dirty tab, tenant admin first then grants then basics', async () => {
    const h = setup();
    await openEditor(h);
    await dirtyAdmin(h);
    await dirtyModel(h);
    await dirtyBasics(h);

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    assert.equal(h.passwordModalOpen(), true, 'a password-protected batch asks before writing');
    await confirmPassword(h);

    assert.deepEqual(writeCalls(h).map(c => c.url), [
        '/api/platform/tenants/tnt-1/admins',
        '/api/platform/tenants/tnt-1/resources',
        '/api/platform/tenants/tnt-1',
    ], 'the batch runs admin -> grants -> basics so a new admin can be enabled in one save');
});

test('each batch step threads the version the previous step returned', async () => {
    const h = setup();
    await openEditor(h);
    await dirtyModel(h);
    await dirtyTool(h);
    await dirtyBasics(h);

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    const puts = grantPuts(h);
    assert.equal(puts.length, 1, 'both grant kinds travel in a single wholesale PUT');
    const grants = bodyOf(puts[0]).grants;
    assert.deepEqual(
        [...new Set(grants.map(g => g.resource_kind))].sort(), ['model', 'tool'],
        'the single PUT carries untouched kinds too, so they are not silently dropped');
    assert.equal(bodyOf(puts[0]).expected_version, 4, 'grant step uses the loaded version');
    const profile = profilePosts(h);
    assert.equal(profile.length, 1);
    assert.equal(bodyOf(profile[0]).expected_version, 5,
        'basics uses the version the grant PUT bumped, not the stale loaded one');
});

test('a failing step stops the batch, keeps what committed and names the step', async () => {
    const h = setup({ grantStatus: 500 });
    await openEditor(h);
    await dirtyAdmin(h);
    await dirtyModel(h);
    await dirtyBasics(h);

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    assert.equal(adminPosts(h).length, 1, 'the admin step committed before the failure');
    assert.equal(grantPuts(h).length, 1, 'the failing grant step was attempted');
    assert.equal(profilePosts(h).length, 0, 'no later step runs after a failure');
    assert.equal(h.node('tenant-editor-error').textContent,
        'tenant_editor_save_failed_step: tenant_tab_model',
        'the operator is told which step failed');
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), true,
        'the uncommitted tabs stay flagged');
    assert.equal(h.node('tenant-status').textContent.includes('admin_saved'), false,
        'a partial batch never claims overall success');
});

test('a fully successful batch clears every dirty mark and reports success', async () => {
    const h = setup();
    await openEditor(h);
    await dirtyAdmin(h);
    await dirtyModel(h);
    await dirtyBasics(h);

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), false,
        'nothing is left owing after every step committed');
    assert.equal(h.passwordModalOpen(), false, 'the prompt is dismissed on success');
    assert.ok(h.node('tenant-status').textContent.includes('admin_saved'),
        'success is reported once the whole batch commits');
});

test('a grants-only save does not ask for a password', async () => {
    const h = setup();
    await openEditor(h);
    await dirtyModel(h);

    h.node('tenant-editor-submit').dispatch('click');
    await flush();

    assert.equal(h.passwordModalOpen(), false,
        'the grant endpoint does not verify the password, so prompting would be theatre');
    assert.equal(grantPuts(h).length, 1, 'the grant save still happens');
});

test('a save touching basics asks for the password before any write', async () => {
    const h = setup();
    await openEditor(h);
    await dirtyBasics(h);

    h.node('tenant-editor-submit').dispatch('click');
    await flush();

    assert.equal(h.passwordModalOpen(), true);
    assert.equal(writeCalls(h).length, 0, 'nothing is written until the password is supplied');
});

test('cancelling the password prompt writes nothing and keeps the draft', async () => {
    const h = setup();
    await openEditor(h);
    await dirtyBasics(h);

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    h.node('tenant-password-cancel').dispatch('click');
    await flush();

    assert.equal(writeCalls(h).length, 0, 'cancelling issues no write at all');
    assert.equal(h.passwordModalOpen(), false);
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), true,
        'the draft survives the cancelled save');
    assert.equal(h.node('tenant-fld-name').value, 'Acme Renamed');
});

test('Escape means the same as cancelling the password prompt', async () => {
    const h = setup();
    await openEditor(h);
    await dirtyBasics(h);

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    assert.equal(h.passwordModalOpen(), true);
    h.ctx.document.dispatch('keydown', { key: 'Escape' });
    await flush();

    assert.equal(h.passwordModalOpen(), false, 'Escape dismisses the prompt');
    assert.equal(writeCalls(h).length, 0, 'Escape writes nothing, exactly like cancel');
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), true,
        'the draft is still owed after Escape');
});

test('the prompt asks again rather than guessing when submitted empty', async () => {
    const h = setup();
    await openEditor(h);
    await dirtyBasics(h);

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    h.node('tenant-password-confirm').dispatch('click');
    await flush();

    assert.equal(h.passwordModalOpen(), true, 'an empty password does not close the prompt');
    assert.equal(h.node('tenant-password-error').textContent, 'tenant_password_required');
    assert.equal(writeCalls(h).length, 0, 'no write is attempted with an empty password');
});

test('a rejected password keeps the prompt open and the draft intact', async () => {
    const h = setup();
    await openEditor(h);
    await dirtyAdmin(h);

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h, 'WrongPassword');

    assert.equal(h.passwordModalOpen(), true, 'the prompt stays open to retry in place');
    assert.equal(h.node('tenant-password-error').textContent, 'tenant_password_wrong');
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), true,
        'nothing committed, so the tab is still owed');
    assert.equal(adminPosts(h).length, 1, 'the failed attempt did reach the server');

    await confirmPassword(h);
    assert.equal(h.passwordModalOpen(), false, 'the retry with the right password succeeds');
    assert.equal(adminPosts(h).length, 2);
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), false);
});

test('the password is never persisted after a successful save', async () => {
    const h = setup();
    await openEditor(h);
    await dirtyBasics(h);

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    assert.equal(h.storageWrites.some(w => w.value.includes('Str0ngAdminPass')), false,
        'the password is not written to any storage');
    const input = h.node('tenant-password-input');
    assert.ok(!input || input.value === '',
        'the collected password does not linger in the DOM');
});

test('the panels no longer carry a standing current-password field', async () => {
    const h = setup();
    await openEditor(h);
    await dirtyAdmin(h);

    assert.equal(h.node('tenant-fld-recent_password'), null,
        'basics has no permanent password input');
    assert.equal(h.node('tenant-fld-admin_recent_password'), null,
        'the tenant-management tab has no permanent password input');
});

test('after creation the editor becomes editable on the basics tab and points at the other tabs', async () => {
    const h = setup();
    await h.ctx.loadTenantView();
    await flush();
    h.node('tenant-create-btn').dispatch('click');
    await flush();
    h.node('tenant-fld-code').value = 'newco';
    h.node('tenant-fld-code').dispatch('input');
    h.node('tenant-fld-name').value = 'NewCo';
    h.node('tenant-fld-name').dispatch('input');
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    assert.equal(h.activeTab(), 'basic', 'creation success stays on the basics tab');
    assert.equal(h.node('tenant-fld-code').readOnly, true, 'the code is fixed once the tenant exists');
    const tabs = [...h.ctx.document.querySelectorAll('.tenant-editor-tab')];
    assert.deepEqual(tabs.map(el => el.getAttribute('data-tab')), ['basic', 'model', 'tool', 'agent', 'admin']);
    assert.equal(tabs.some(el => el.disabled), false, 'the other tabs become usable');
    assert.equal(h.node('tenant-editor-foot-hint').textContent, 'tenant_editor_created_hint');
});

// ---- 5.x admin tab: select an existing account or create a new one --------

const adminModes = h => [...h.ctx.document.querySelectorAll('.tenant-admin-mode')]
    .map(el => el.getAttribute('data-mode'));

const candidateNames = h => [...h.ctx.document.querySelectorAll('.user-picker-option')]
    .map(el => el.getAttribute('data-user-name'));

async function chooseNewMode(h) {
    h.node('tenant-admin-mode-new').dispatch('click');
    await flush();
}

async function fillNewAdmin(h, over = {}) {
    const values = {
        'tenant-fld-admin_new_username': 'acme-admin',
        'tenant-fld-admin_new_display': 'Acme Admin',
        'tenant-fld-admin_new_password': 'Str0ngTempPass',
        ...over,
    };
    for (const id of Object.keys(values)) {
        const el = h.node(id);
        el.value = values[id];
        el.dispatch('input');
    }
    await flush();
}

test('the admin tab offers select-existing and create-new modes, defaulting to select', async () => {
    const h = setup();
    await openAdminTab(h);

    assert.deepEqual(adminModes(h), ['existing', 'new'],
        'both ways of appointing an admin are offered');
    assert.equal(h.node('tenant-admin-mode-existing').classList.contains('active'), true,
        'selecting an existing account is the default');
    assert.equal(h.node('tenant-admin-mode-new').classList.contains('active'), false);
    assert.equal(h.node('tenant-admin-body-existing').classList.contains('hidden'), false);
    assert.equal(h.node('tenant-admin-body-new').classList.contains('hidden'), true,
        'the create form is out of the way until it is chosen');
});

test('select mode submits mode=existing with the picked id and no create-only fields', async () => {
    const h = setup();
    await openAdminTab(h);
    h.ctx.document.querySelector('.user-picker-option').dispatch('click');
    await flush();
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    const body = bodyOf(adminPosts(h)[0]);
    assert.equal(body.mode, 'existing', 'the pre-existing behaviour is named explicitly');
    assert.equal(body.user_id, 'usr-root', 'the picked account travels as its stable id');
    assert.equal(body.username, undefined, 'select mode never sends create-only fields');
    assert.equal(body.temporary_password, undefined,
        'select mode never asks the server to set a password');
});

test('create mode submits mode=new with username, display name and initial password', async () => {
    const h = setup();
    await openAdminTab(h);
    await chooseNewMode(h);
    await fillNewAdmin(h);
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    const body = bodyOf(adminPosts(h)[0]);
    assert.equal(body.mode, 'new');
    assert.equal(body.username, 'acme-admin');
    assert.equal(body.display_name, 'Acme Admin');
    assert.equal(body.temporary_password, 'Str0ngTempPass');
    assert.equal(body.user_id, undefined,
        'create mode must never be mistaken for a request to bind an existing account');
});

test('create mode blocks submit until username, display name and initial password are all present', async () => {
    const h = setup();
    await openAdminTab(h);
    await chooseNewMode(h);
    // Put the tab in the batch without filling anything, so the failure under
    // test is the incomplete form and not an empty draft.
    h.node('tenant-fld-admin_new_username').dispatch('input');
    await flush();

    const order = ['tenant-fld-admin_new_username', 'tenant-fld-admin_new_display',
                   'tenant-fld-admin_new_password'];
    const filled = { 'tenant-fld-admin_new_username': 'acme-admin',
                     'tenant-fld-admin_new_display': 'Acme Admin',
                     'tenant-fld-admin_new_password': 'Str0ngTempPass' };
    const blames = ['tenant_admin_new_username_required',
                    'tenant_admin_new_display_required',
                    'tenant_admin_new_password_required'];

    for (let missing = 0; missing < order.length; missing++) {
        order.forEach((id, i) => {
            h.node(id).value = i < missing ? filled[id] : '';
            h.node(id).dispatch('input');
        });
        h.node('tenant-editor-submit').dispatch('click');
        await flush();

        assert.equal(h.node('tenant-editor-error').textContent, blames[missing],
            'the error names the field that is still missing: ' + order[missing]);
        assert.equal(adminPosts(h).length, 0, 'an incomplete create form writes nothing');
        assert.equal(h.passwordModalOpen(), false,
            'the password prompt is not reached before the form is complete');
    }
});

test('switching admin modes keeps what the other mode already had', async () => {
    const h = setup();
    await openAdminTab(h);
    h.node('tenant-fld-admin_display').value = 'Renamed Member';
    h.node('tenant-fld-admin_display').dispatch('input');

    await chooseNewMode(h);
    await fillNewAdmin(h, { 'tenant-fld-admin_new_username': 'second-admin' });

    h.node('tenant-admin-mode-existing').dispatch('click');
    await flush();
    assert.equal(h.node('tenant-fld-admin_display').value, 'Renamed Member',
        'returning to select mode keeps the display name that was typed there');
    assert.equal(h.node('tenant-admin-body-new').classList.contains('hidden'), true);

    await chooseNewMode(h);
    assert.equal(h.node('tenant-fld-admin_new_username').value, 'second-admin',
        'the create form keeps its username across a mode round trip');
    assert.equal(h.node('tenant-fld-admin_new_display').value, 'Acme Admin');
    assert.equal(h.node('tenant-fld-admin_new_password').value, 'Str0ngTempPass');
});

test('picking an account prefills its display name and still lets the operator rename it', async () => {
    const h = setup();
    await openAdminTab(h);
    h.ctx.document.querySelector('.user-picker-option').dispatch('click');
    await flush();

    assert.equal(h.node('tenant-fld-admin_display').value, 'Root',
        'the member display name starts from the account that was picked, not a blank box');
    // ...and the prefill is a starting point, not a lock.
    h.node('tenant-fld-admin_display').value = 'Root Renamed';
    h.node('tenant-fld-admin_display').dispatch('input');
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    const body = bodyOf(adminPosts(h)[0]);
    assert.equal(body.user_id, 'usr-root');
    assert.equal(body.display_name, 'Root Renamed', 'the edited name is what gets saved');
});

test('a taken username keeps the create form and points at the select mode instead', async () => {
    const h = setup({ usernameTaken: true });
    await openAdminTab(h);
    await chooseNewMode(h);
    await fillNewAdmin(h);
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    assert.equal(h.node('tenant-editor-error').textContent, 'tenant_admin_username_taken',
        'the operator is told the name is taken, not just that a step failed');
    assert.equal(h.node('tenant-fld-admin_new_username').value, 'acme-admin',
        'the form keeps its input so switching to select mode is a one-click fix');
    assert.equal(h.node('tenant-fld-admin_new_display').value, 'Acme Admin');
    assert.equal(h.node('tenant-fld-admin_new_password').value, 'Str0ngTempPass');
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), true,
        'the admin is still owed, so the tab stays unsaved');
});

test('after creating an account the tab is clean and the candidate set can find it', async () => {
    const h = setup();
    await openAdminTab(h);
    await chooseNewMode(h);
    await fillNewAdmin(h);
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);
    await flush();

    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), false,
        'the account was committed, so nothing is left unsaved');
    assert.ok(candidateNames(h).includes('Acme Admin'),
        'the candidate set was reloaded, so the new admin is pickable without a page reload');
});

// ---- 5.y the tab shows the tenant's current admin on open -----------------
// A tenant that already has an admin must say so, otherwise the operator sees
// an empty picker and cannot tell "nobody administers this" from "somebody
// does". The display is read-only: it must not preselect the picker or dirty
// the tab, so a read never turns into an accidental write.

const currentAdminHtml = h => h.node('tenant-current-admin').innerHTML;

test('the admin tab shows the current admin read-only without preselecting', async () => {
    const h = setup({ currentAdmins: [
        { membership_id: 'mem-1', user_id: 'usr-root', username: 'root', display_name: 'Root Ops' },
        { membership_id: 'mem-2', user_id: 'usr-alice', username: 'alice', display_name: 'Alice' },
    ]});
    await openAdminTab(h);

    const html = currentAdminHtml(h);
    assert.ok(html.includes('Root Ops'), 'the earliest admin display name is shown');
    assert.ok(html.includes('root'), 'the login name disambiguates same-named accounts');
    assert.equal(html.includes('Alice'), false, 'only the earliest admin is shown by default');
    assert.equal(
        h.node('tenant-admin-picker').querySelector('.user-picker-selected').innerHTML, '',
        'showing the admin must not preselect the picker');
    assert.equal(h.node('tenant-fld-admin_display').value, '',
        'showing the admin must not prefill the editable display name');
});

test('a tenant with no admin says so instead of inventing one', async () => {
    const h = setup({ currentAdmins: [] });
    await openAdminTab(h);
    assert.ok(currentAdminHtml(h).includes('tenant_current_admin_none'));
});

test('a failed current-admin read is not reported as "no admin"', async () => {
    const h = setup({ adminReadStatus: 500 });
    await openAdminTab(h);
    const html = currentAdminHtml(h);
    assert.ok(html.includes('tenant_current_admin_unavailable'),
        'a read failure is distinguished from an empty admin set');
    assert.equal(html.includes('tenant_current_admin_none'), false,
        'an unreadable admin must not be shown as "no admin"');
});

test('showing the current admin does not make the tab unsaved', async () => {
    const h = setup({ currentAdmins: [
        { membership_id: 'mem-1', user_id: 'usr-root', username: 'root', display_name: 'Root' },
    ]});
    await openAdminTab(h);

    assert.ok(h.node('tenant-current-admin'), 'the current-admin block is rendered');
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), false,
        'a read must not dirty the tab');
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    assert.equal(adminPosts(h).length, 0, 'a clean tab writes nothing');
});

test('a committed admin change refreshes the read-only current admin', async () => {
    const h = setup();  // starts with no admin
    await openAdminTab(h);
    assert.ok(currentAdminHtml(h).includes('tenant_current_admin_none'));

    h.ctx.document.querySelector('.user-picker-option').dispatch('click');
    await flush();
    h.node('tenant-fld-admin_display').value = 'Root Ops';
    h.node('tenant-fld-admin_display').dispatch('input');
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);
    await flush();

    assert.ok(currentAdminHtml(h).includes('Root Ops'),
        'the just-committed admin is what the tab reports');
});

// ---- 5.z the card's own account enable/disable and reset controls ---------
// These two controls act on the account the card *shows*. They are separate
// from the tab's save: they must not read or commit the picker's unsaved
// selection, and they must not change the platform qualification they were
// handed. A disabled account stays on the card so it can be re-enabled there.

function toggleCalls(h) {
    return h.calls.filter(c => c.options && c.options.method === 'PATCH'
        && /^\/api\/platform\/users\/[^/]+$/.test(c.url));
}
function resetCalls(h) {
    return h.calls.filter(c => c.options && c.options.method === 'POST'
        && /^\/api\/platform\/users\/[^/]+\/password$/.test(c.url));
}
const activeAdmin = {
    membership_id: 'mem-1', user_id: 'usr-root', username: 'root',
    display_name: 'Root', account_active: true, account_version: 3,
    is_platform_admin: true,
};

test('the card offers disable and reset for an active account', async () => {
    const h = setup({ currentAdmins: [{ ...activeAdmin }] });
    await openAdminTab(h);
    const html = currentAdminHtml(h);
    assert.ok(html.includes('tenant_admin_disable_account'), 'an active account offers disable');
    assert.ok(html.includes('tenant_admin_account_active'), 'the card names the account state');
    assert.ok(html.includes('admin_reset'), 'the card offers reset');
    assert.equal(html.includes('tenant_admin_enable_account'), false,
        'the disable control is not offered while the account is active');
});

test('a disabled card account offers enable instead of disable', async () => {
    const h = setup({ currentAdmins: [{ ...activeAdmin, account_active: false }] });
    await openAdminTab(h);
    const html = currentAdminHtml(h);
    assert.ok(html.includes('tenant_admin_enable_account'), 'a disabled account offers enable');
    assert.ok(html.includes('tenant_admin_account_disabled'), 'the card marks it disabled');
    assert.equal(html.includes('tenant_admin_disable_account'), false,
        'the enable control replaces disable rather than duplicating it');
});

test('an unreadable or absent admin gets no account controls', async () => {
    const failed = setup({ adminReadStatus: 500 });
    await openAdminTab(failed);
    assert.equal(currentAdminHtml(failed).includes('tenant_admin_disable_account'), false,
        'a failed read offers no account controls');
    assert.equal(currentAdminHtml(failed).includes('admin_reset'), false);

    const empty = setup({ currentAdmins: [] });
    await openAdminTab(empty);
    assert.equal(currentAdminHtml(empty).includes('admin_reset'), false,
        'no admin means no account to act on');
});

test('disabling the card account submits its version and original platform qualification', async () => {
    const h = setup({ currentAdmins: [{ ...activeAdmin }] });
    await openAdminTab(h);
    h.ctx.tenantCardToggleAccount();
    await flush();
    assert.equal(h.modalOpen(), true, 'the operator password is re-checked before the write');
    h.node('adm-fld-recent_password').value = 'Str0ngAdminPass';
    h.node('admin-modal-submit').dispatch('click');
    await flush();

    assert.equal(toggleCalls(h).length, 1, 'the account status is written once');
    assert.equal(toggleCalls(h)[0].url, '/api/platform/users/usr-root');
    const body = JSON.parse(toggleCalls(h)[0].options.body);
    assert.equal(body.active, false, 'the toggle disables the shown account');
    assert.equal(body.is_platform_admin, true,
        'the original platform qualification is preserved, never dropped');
    assert.equal(body.expected_version, 3, 'the account version guards the write');
    assert.equal(body.recent_password, 'Str0ngAdminPass');
    assert.ok(currentAdminHtml(h).includes('tenant_admin_enable_account'),
        'the card refreshes to the disabled state so it can be re-enabled');
});

test('a disabled account can be re-enabled from the refreshed card', async () => {
    const h = setup({ currentAdmins: [{ ...activeAdmin, account_active: false }] });
    await openAdminTab(h);
    h.ctx.tenantCardToggleAccount();
    await flush();
    h.node('adm-fld-recent_password').value = 'Str0ngAdminPass';
    h.node('admin-modal-submit').dispatch('click');
    await flush();
    const body = JSON.parse(toggleCalls(h)[0].options.body);
    assert.equal(body.active, true, 'the enable writes the active flag back');
    assert.ok(currentAdminHtml(h).includes('tenant_admin_disable_account'));
});

test('the card controls never target the picker\u2019s unsaved selection', async () => {
    const h = setup({ currentAdmins: [{ ...activeAdmin }] });
    await openAdminTab(h);
    // Pick another candidate, leaving the tab dirty: this is a draft, not a
    // committed choice, and the card buttons must ignore it.
    const option = h.ctx.document.querySelector('.user-picker-option');
    assert.ok(option, 'a different candidate is available to pick');
    option.dispatch('click');
    await flush();
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), true,
        'picking a candidate leaves an unsaved draft');

    h.ctx.tenantCardResetPassword();
    await flush();
    h.node('adm-fld-recent_password').value = 'Str0ngAdminPass';
    h.node('admin-modal-submit').dispatch('click');
    await flush();

    assert.equal(resetCalls(h).length, 1, 'the reset runs for the card account');
    assert.equal(resetCalls(h)[0].url, '/api/platform/users/usr-root/password',
        'the target is the card account, never the unsaved picker selection');
    assert.equal(JSON.parse(resetCalls(h)[0].options.body).expected_version, 3,
        'the reset uses the card account version');
    assert.equal(profilePosts(h).length, 0, 'the card action commits no tenant draft');
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), true,
        'the picker draft is preserved, not discarded');
});

test('a reset from the card keeps its one-time password visible and clears on close', async () => {
    const h = setup({ currentAdmins: [{ ...activeAdmin }] });
    await openAdminTab(h);
    h.ctx.tenantCardResetPassword();
    await flush();
    h.node('adm-fld-recent_password').value = 'Str0ngAdminPass';
    h.node('admin-modal-submit').dispatch('click');
    await flush();

    assert.equal(h.modalOpen(), false, 'the password prompt is dismissed on success');
    const dialog = h.node('one-time-secret-modal');
    assert.ok(dialog && !dialog.classList.contains('hidden'),
        'the one-time result is not closed with the generic submit');
    assert.equal(h.node('one-time-secret-value').textContent, 'Tmp-Pass-123',
        'the returned temporary password is shown for copying');
    assert.equal(h.storageWrites.some(w => String(w.value).includes('Tmp-Pass-123')), false,
        'the password is never written to browser storage');

    h.node('one-time-secret-close').dispatch('click');
    assert.equal(h.node('one-time-secret-modal').classList.contains('hidden'), true);
    assert.equal(h.node('one-time-secret-value').textContent, '',
        'closing clears the password from the page');
});

test('a one-time password does not survive a context switch', async () => {
    // The secret belongs to the account of the context that produced it: a
    // tenant switch / identity change (bumpTenantGeneration) must drop it so it
    // cannot reappear over the next tenant, and closing the editor clears it too.
    const h = setup({ currentAdmins: [{ ...activeAdmin }] });
    await openAdminTab(h);
    h.ctx.tenantCardResetPassword();
    await flush();
    h.node('adm-fld-recent_password').value = 'Str0ngAdminPass';
    h.node('admin-modal-submit').dispatch('click');
    await flush();
    assert.equal(h.node('one-time-secret-value').textContent, 'Tmp-Pass-123');

    h.ctx.bumpTenantGeneration();
    assert.equal(h.node('one-time-secret-modal').classList.contains('hidden'), true,
        'leaving the context hides a result that belonged to it');
    assert.equal(h.node('one-time-secret-value').textContent, '',
        'the secret is dropped from the page, not merely hidden');
});

test('a failed card refresh after a successful reset does not hide the password', async () => {
    const h = setup({ currentAdmins: [{ ...activeAdmin }], adminReadFailAfterFirst: true });
    await openAdminTab(h);
    h.ctx.tenantCardResetPassword();
    await flush();
    h.node('adm-fld-recent_password').value = 'Str0ngAdminPass';
    h.node('admin-modal-submit').dispatch('click');
    await flush();

    assert.equal(h.node('one-time-secret-value').textContent, 'Tmp-Pass-123',
        'a refresh failure must not erase a password the server already returned');
});

test('a stale card toggle after leaving does not overwrite the next target', async () => {
    const h = setup({ currentAdmins: [{ ...activeAdmin }], deferToggle: true });
    await openAdminTab(h);
    h.ctx.tenantCardToggleAccount();
    await flush();
    h.node('adm-fld-recent_password').value = 'Str0ngAdminPass';
    h.node('admin-modal-submit').dispatch('click');
    await flush();

    // The operator switches tenant generation before the reply lands.
    h.ctx.bumpTenantGeneration();
    h.deferToggle();
    await flush();
    assert.equal(h.node('tenant-editor').classList.contains('hidden'), false,
        'the late reply does not throw the editor away');
});

test('resetting your own account guides to the self change-password flow', async () => {
    const h = setup({ currentAdmins: [
        { ...activeAdmin, user_id: 'u-root', username: 'root' },
    ]});
    await openAdminTab(h);
    h.ctx.tenantCardResetPassword();
    await flush();

    assert.equal(resetCalls(h).length, 0, 'a self reset is not submitted');
    assert.equal(h.modalOpen(), false, 'no password prompt is opened for a self reset');
    assert.ok(h.node('tenant-current-admin-msg').textContent.includes('account_self_reset_hint'),
        'the operator is guided to the existing self change-password flow');
});

// ---- the server's reason must reach the operator --------------------------
// The initial password is rejected below MIN_PASSWORD_LENGTH (8) by the
// service. Reporting only "failed step: tenant admin" leaves the operator with
// no idea what to change, which is what this guards.

test('a weak initial password is explained instead of blamed on the step', async () => {
    const h = setup({ adminError: { code: 'weak_password', message: 'weak temporary password', status: 400 } });
    await openAdminTab(h);
    await chooseNewMode(h);
    await fillNewAdmin(h, { 'tenant-fld-admin_new_password': '123456' });
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    const shown = h.node('tenant-editor-error').textContent;
    assert.equal(shown, 'tenant_admin_weak_password',
        'the message names the password rule, not the step that happened to fail');
    assert.ok(!shown.includes('tenant_editor_save_failed_step'),
        'a known reason must not degrade to the generic step failure');
    assert.equal(h.node('tenant-fld-admin_new_password').value, '123456',
        'the operator keeps what they typed so they can lengthen it');
});

test('an invalid username is explained instead of blamed on the step', async () => {
    const h = setup({ adminError: { code: 'invalid_username', message: 'invalid username', status: 400 } });
    await openAdminTab(h);
    await chooseNewMode(h);
    await fillNewAdmin(h);
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    assert.equal(h.node('tenant-editor-error').textContent, 'tenant_admin_invalid_username',
        'an illegal username says so, rather than reporting a nameless step failure');
});

test('a weak-password rejection survives even when basics rides along in the batch', async () => {
    const h = setup({ adminError: { code: 'weak_password', message: 'weak temporary password', status: 400 } });
    await openEditor(h);
    await dirtyAdminNew(h);
    await dirtyBasics(h);
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);
    await flush();

    assert.equal(h.node('tenant-editor-error').textContent, 'tenant_admin_weak_password',
        'the reason is still named when the admin step is only one of several');
    assert.equal(writeCalls(h).filter(c => c.url === '/api/platform/tenants/tnt-1').length, 0,
        'the later basic-info step is still skipped after the failure');
});

test('an unrecognised failure still names the step, so nothing regresses to silence', async () => {
    const h = setup({ adminError: { code: 'something_new', message: 'boom', status: 500 } });
    await openAdminTab(h);
    await chooseNewMode(h);
    await fillNewAdmin(h);
    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    const shown = h.node('tenant-editor-error').textContent;
    assert.ok(shown.includes('tenant_editor_save_failed_step'),
        'an unmapped failure still reports which step broke');
    assert.ok(shown.includes('tenant_tab_admin'), 'and it says which step that was');
});

test('the create form states the password rule up front, like the member form does', async () => {
    const h = setup();
    await openAdminTab(h);
    await chooseNewMode(h);

    const hint = h.node('tenant-hint-admin_new_password');
    assert.ok(hint, 'the initial-password field carries a hint');
    assert.equal(hint.textContent, 'admin_field_password_hint',
        'it reuses the existing account-creation hint rather than inventing a second rule');
});

async function dirtyAdminNew(h) {
    await openAdminTab(h);
    await chooseNewMode(h);
    await fillNewAdmin(h);
}


// ---- 9.x agent tab (copy the source tenant's agents into this one) --------

async function openAgentTab(h) {
    await openEditor(h);
    h.node('tenant-editor-tab-agent').dispatch('click');
    await flush();
}

function agentCopyPosts(h) {
    return h.calls.filter(c => c.url === '/api/platform/tenants/tnt-1/agents'
        && c.options && c.options.method === 'POST');
}

function agentReads(h) {
    return h.calls.filter(c => c.url === '/api/platform/tenants/tnt-1/agents'
        && (!c.options || !c.options.method || c.options.method === 'GET'));
}

function candidateBoxes(h) {
    const host = h.node('tenant-agent-candidates');
    // Spread: the production code must not be handed an Array here, or the
    // NodeList/Array distinction the bug hid behind disappears again.
    return host ? [...host.querySelectorAll('input[type=checkbox]')] : [];
}

function checkCandidate(h, sourceId) {
    const box = candidateBoxes(h).find(b => b.value === sourceId);
    assert.ok(box, 'the candidate ' + sourceId + ' is offered');
    box.checked = true;
    box.dispatch('change');
}

test('the editor adds an agent tab between tool and admin', async () => {
    const h = setup();
    await openEditor(h);

    assert.deepEqual(h.editorTabs(), ['basic', 'model', 'tool', 'agent', 'admin'],
        'the new tab is inserted without disturbing the existing order');
    assert.equal(h.node('tenant-panel-agent') != null, true,
        'the panel exists and is addressable like the others');
});

test('the agent tab lists the source candidates and marks the already-copied ones', async () => {
    const h = setup();
    await openAgentTab(h);

    assert.equal(agentReads(h).length, 1, 'the tab reads the agents once');
    const boxes = candidateBoxes(h);
    assert.deepEqual(boxes.map(b => b.value), ['alpha', 'beta', 'gamma'],
        'every copyable source agent is a checkbox');
    const beta = boxes.find(b => b.value === 'beta');
    assert.equal(beta.disabled, true,
        'an already-synced candidate cannot be picked a second time');
    const alpha = boxes.find(b => b.value === 'alpha');
    assert.equal(alpha.disabled, false, 'a fresh candidate stays selectable');
});

test('picking a candidate refreshes the counter instead of dying in the handler', async () => {
    // Regression: the checkbox handler called a filter on a NodeList, threw, and
    // left the counter blank. Save then threw in validation the same way, which
    // is why clicking it looked like nothing happened. The counter is the
    // operator-visible proof that the handler ran to completion.
    const h = setup();
    await openAgentTab(h);
    assert.equal(h.node('tenant-agent-selected').textContent, '已选 0 个',
        'the counter starts at zero');

    checkCandidate(h, 'alpha');

    assert.equal(h.node('tenant-agent-selected').textContent, '已选 1 个',
        'picking a candidate refreshes the count');
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), true,
        'and the draft is flagged unsaved so Save is willing to run');
});

test('the agent tab shows the tenant own agents read-only with a default marker', async () => {
    const h = setup({ targetAgents: [
        { id: 'assistant-acme', name: 'Assistant', enabled: true, is_default: true },
        { id: 'retired-acme', name: 'Retired', enabled: false, is_default: false },
    ] });
    await openAgentTab(h);

    const html = h.node('tenant-agent-current').innerHTML;
    assert.ok(html.includes('Assistant') && html.includes('assistant-acme'),
        'the name and id of a bound agent are shown');
    assert.ok(html.includes('retired-acme'), 'a disabled agent is still listed');
    assert.ok(html.includes('tenant_agent_default_badge'),
        'the default agent is marked');
    assert.ok(html.includes('tenant_agent_enabled') && html.includes('tenant_agent_disabled'),
        'enabled state is rendered, not implied');
});

test('a tenant with no agents says so, and a failed read is not reported as none', async () => {
    const empty = setup();
    await openAgentTab(empty);
    assert.ok(empty.node('tenant-agent-current').innerHTML.includes('tenant_agent_current_empty'),
        'an empty target is stated as empty');

    const broken = setup({ agentReadStatus: 500 });
    await openAgentTab(broken);
    const html = broken.node('tenant-agent-current').innerHTML;
    assert.ok(html.includes('tenant_agent_current_unavailable'),
        'an unreadable list is surfaced as a failure');
    assert.equal(html.includes('tenant_agent_current_empty'), false,
        'a failure is never disguised as "nothing here"');
});

test('an unresolvable source is explained without losing the tenant own list', async () => {
    const h = setup({ copySource: 'none', targetAgents: [
        { id: 'assistant-acme', name: 'Assistant', enabled: true, is_default: true },
    ] });
    await openAgentTab(h);

    assert.ok(h.node('tenant-agent-current').innerHTML.includes('assistant-acme'),
        'the tenant state still renders when nobody can be a source');
    assert.ok(h.node('tenant-agent-candidates').innerHTML.includes('tenant_agent_source_unavailable'),
        'the copy block explains why there is nothing to pick from');
});

test('submitting after unchecking everything sends no request and says what is missing', async () => {
    const h = setup();
    await openAgentTab(h);
    checkCandidate(h, 'alpha');
    await flush();
    // Check then uncheck: the tab is still part of the batch, but there is no
    // longer a selection. That must never degrade into "copy everything".
    const box = candidateBoxes(h).find(b => b.value === 'alpha');
    box.checked = false;
    box.dispatch('change');

    h.node('tenant-editor-submit').dispatch('click');
    await flush();

    assert.equal(agentCopyPosts(h).length, 0, 'no copy request without a selection');
    assert.equal(h.node('tenant-editor-error').textContent, 'tenant_agent_pick_required',
        'the operator is told to pick at least one agent');
    assert.equal(h.passwordModalOpen(), false,
        'an incomplete form is rejected before asking for a password');
});

test('checking a candidate copies exactly the picked ids and echoes the result', async () => {
    const h = setup();
    await openAgentTab(h);
    checkCandidate(h, 'alpha');
    checkCandidate(h, 'gamma');
    await flush();

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    const posts = agentCopyPosts(h);
    assert.equal(posts.length, 1, 'the selection is copied in one request');
    const body = JSON.parse(posts[0].options.body);
    assert.equal(body.action, 'copy');
    assert.deepEqual(body.source_agent_ids, ['alpha', 'gamma'],
        'only the checked candidates travel');
    assert.equal(body.recent_password, 'Str0ngAdminPass');

    const result = h.node('tenant-agent-result').innerHTML;
    assert.ok(result.includes('tenant_agent_result_copied'), 'the copied count is echoed');
    assert.ok(result.includes('alpha-acme'), 'the new clone id is named');
    const current = h.node('tenant-agent-current').innerHTML;
    assert.ok(current.includes('alpha-acme'),
        'the read-only list reflects what this save just added');
});

test('a partial copy keeps the tab dirty, retryable and never claims success', async () => {
    const h = setup({ agentCopyFails: ['alpha'] });
    await openAgentTab(h);
    checkCandidate(h, 'alpha');
    checkCandidate(h, 'gamma');
    await flush();

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    assert.equal(agentCopyPosts(h).length, 1, 'the copy was attempted');
    assert.equal(h.node('tenant-editor-error').textContent, 'tenant_agent_partial_failed',
        'the failure is named rather than swallowed');
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), true,
        'the uncommitted agent stays owed');
    assert.equal(h.node('tenant-status').textContent.includes('admin_saved'), false,
        'a partial copy never reports overall success');
    assert.equal(
        candidateBoxes(h).find(b => b.value === 'alpha').checked, true,
        'the failed candidate stays checked so it can simply be retried');
});

test('the batch copies agents after grants and before basics, without touching the version', async () => {
    const h = setup();
    await openEditor(h);
    await dirtyModel(h);
    h.node('tenant-editor-tab-agent').dispatch('click');
    await flush();
    checkCandidate(h, 'alpha');
    await flush();
    await dirtyBasics(h);

    h.node('tenant-editor-submit').dispatch('click');
    await flush();
    await confirmPassword(h);

    assert.deepEqual(writeCalls(h).map(c => c.url), [
        '/api/platform/tenants/tnt-1/resources',
        '/api/platform/tenants/tnt-1/agents',
        '/api/platform/tenants/tnt-1',
    ], 'the agent step sits between grants and basics');
    const copy = JSON.parse(agentCopyPosts(h)[0].options.body);
    assert.equal('expected_version' in copy, false,
        'copying agents does not consume the tenant version');
    assert.equal(bodyOf(profilePosts(h)[0]).expected_version, 5,
        'basics still uses the version the grant PUT bumped');
});

test('switching tabs keeps the checked candidates as a draft', async () => {
    const h = setup();
    await openAgentTab(h);
    checkCandidate(h, 'alpha');
    await flush();
    assert.equal(h.node('tenant-dirty-pill').classList.contains('show'), true,
        'picking an agent flags the draft');

    h.node('tenant-editor-tab-basic').dispatch('click');
    await flush();
    h.node('tenant-editor-tab-agent').dispatch('click');
    await flush();

    assert.equal(candidateBoxes(h).find(b => b.value === 'alpha').checked, true,
        'the picked candidate survives a tab switch');
    assert.equal(agentReads(h).length, 1,
        'reopening the tab does not re-read and discard the draft');
});

test('create mode explains the agent tab and writes nothing', async () => {
    const h = setup();
    await h.ctx.loadTenantView();
    await flush();
    h.node('tenant-create-btn').dispatch('click');
    await flush();

    const btn = h.node('tenant-editor-tab-agent');
    assert.equal(btn.disabled, true,
        'a tenant that does not exist yet cannot receive agents');
    h.node('tenant-editor-tab-agent').dispatch('click');
    await flush();

    assert.equal(h.activeTab(), 'basic', 'the click does not leave the basics tab');
    assert.equal(agentCopyPosts(h).length, 0, 'nothing is written in create mode');
    assert.equal(agentReads(h).length, 0, 'nor is a guess about a source made');
});
