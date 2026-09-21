// Execute the shipped account/authentication handlers with controlled requests.
// Browser acceptance owns layout; these tests make identity races reproducible.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createAppearanceController } = require('../channel/web/static/js/appearance.js');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
function section(start, end) {
    const from = source.indexOf(start);
    assert.ok(from >= 0, `Missing console section ${start}`);
    // An absent `end` means "to the end of the file", which is what the last
    // section needs: the scheduled-task sections that used to follow
    // Initialization moved to the fork patch module (change
    // port-upstream-tasks-page), so nothing marks its end any more.
    const to = end === undefined
        ? source.length
        : source.indexOf(end, from + start.length);
    assert.ok(to > from, `Missing console section ${end}`);
    return source.slice(from, to);
}

function eventTarget() {
    const listeners = new Map();
    return {
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, new Set());
            listeners.get(type).add(fn);
        },
        removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
        dispatch(type, extra = {}) {
            const event = { type, target: this, preventDefault() {}, stopPropagation() {}, ...extra };
            for (const fn of [...(listeners.get(type) || [])]) fn.call(this, event);
            return event;
        },
        listenerCount(type) { return listeners.get(type)?.size || 0; },
    };
}

function element(document, tag = 'div') {
    const classes = new Set();
    let text = '', html = '';
    const el = {
        ...eventTarget(), tagName: tag.toUpperCase(), id: '', attrs: {}, dataset: {}, style: {},
        children: [], parentNode: null, disabled: false, hidden: false, value: '', title: '',
        get className() { return [...classes].join(' '); },
        set className(value) { classes.clear(); String(value).split(/\s+/).filter(Boolean).forEach(v => classes.add(v)); },
        get textContent() { return text; },
        set textContent(value) { text = String(value); html = ''; this.children = []; },
        get innerHTML() { return html; },
        set innerHTML(value) { html = String(value); text = ''; this.children = []; },
        get firstChild() { return this.children[0] || null; },
        get firstElementChild() { return this.firstChild; },
        get offsetParent() { return this.hidden || this.classList.contains('hidden') ? null : this.parentNode || document.body; },
        classList: {
            add: (...names) => names.forEach(name => classes.add(name)),
            remove: (...names) => names.forEach(name => classes.delete(name)),
            contains: name => classes.has(name),
            replace(a, b) { if (classes.delete(a)) classes.add(b); },
            toggle(name, force) {
                const add = force === undefined ? !classes.has(name) : force;
                if (add) classes.add(name); else classes.delete(name);
                return add;
            },
        },
        setAttribute(key, value) { this.attrs[key] = String(value); },
        getAttribute(key) { return this.attrs[key] ?? null; },
        removeAttribute(key) { delete this.attrs[key]; },
        appendChild(child) {
            child.parentNode?.removeChild(child);
            this.children.push(child); child.parentNode = this;
            if (this.tagName === 'SELECT' && (!this.value || child.selected)) this.value = child.value;
            return child;
        },
        removeChild(child) { this.children = this.children.filter(c => c !== child); child.parentNode = null; },
        replaceChildren(...children) { this.children = []; children.forEach(child => this.appendChild(child)); },
        contains(other) { return this === other || this.children.some(child => child.contains(other)); },
        matches(selector) {
            if (selector.includes(':disabled') && selector.includes(':not(') && this.disabled) return false;
            if (selector.includes('[hidden]') && selector.includes(':not(') && this.hidden) return false;
            if (selector.includes('.hidden') && selector.includes(':not(') && this.classList.contains('hidden')) return false;
            const simple = selector.replace(/:not\([^)]*\)/g, '').trim();
            if (simple.startsWith('#')) return this.id === simple.slice(1);
            if (simple.startsWith('.')) return this.classList.contains(simple.slice(1));
            if (simple.startsWith('[tabindex')) return this.attrs.tabindex !== undefined;
            return this.tagName.toLowerCase() === simple.replace(/\[.*$/, '').toLowerCase();
        },
        querySelectorAll(selector) {
            const selectors = selector.split(',').map(s => s.trim());
            return this.children.flatMap(child => [
                ...(selectors.some(s => child.matches(s)) ? [child] : []), ...child.querySelectorAll(selector),
            ]);
        },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
        closest(selector) { return this.matches(selector) ? this : this.parentNode?.closest(selector) || null; },
        getClientRects() { return this.offsetParent ? [{}] : []; },
        getBoundingClientRect() { return { top: 780, left: 0, width: 224, height: 72, bottom: 852 }; },
        focus() { document.activeElement = this; },
    };
    return el;
}

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}
const settle = () => new Promise(resolve => setImmediate(resolve));
const response = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const database = (username = 'alice', extra = {}) => ({ status: 'success', identity_mode: 'database',
    auth_required: true, authenticated: true, user: { username, display_name: username.toUpperCase() }, ...extra });

function setup(transport = async () => response(database()), { agentWrapper = false, tenantResolution = false } = {}) {
    const nodes = new Map(), calls = [], storage = new Map(), timers = [];
    const document = { ...eventTarget(), activeElement: null };
    document.body = element(document, 'body');
    const node = id => {
        if (!nodes.has(id)) {
            const tag = /(?:btn|retry|logout|trigger)$/.test(id) ? 'button' : id.endsWith('select') ? 'select' : 'div';
            const el = element(document, tag); el.id = id; nodes.set(id, el);
        }
        return nodes.get(id);
    };
    Object.assign(document, {
        getElementById: node, createElement: tag => element(document, tag),
        querySelector: () => null, querySelectorAll: () => [],
    });
    document.body.appendChild(node('app'));
    node('app').appendChild(node('sidebar'));
    node('sidebar').appendChild(node('sidebar-account-footer'));
    const footer = node('sidebar-account-footer');
    footer.appendChild(node('sidebar-account-toggle'));
    footer.appendChild(node('sidebar-account-menu'));
    node('sidebar-account-toggle').tagName = 'BUTTON';
    node('sidebar-account-toggle').setAttribute('aria-expanded', 'false');
    for (const id of ['sidebar-account-avatar', 'sidebar-account-avatar-icon', 'sidebar-account-name', 'sidebar-account-subtitle']) {
        node('sidebar-account-toggle').appendChild(node(id));
    }
    for (const id of ['account-menu-identity', 'account-menu-status', 'account-menu-retry', 'account-menu-logout', 'sidebar-version']) {
        node('sidebar-account-menu').appendChild(node(id));
    }
    node('account-menu-identity').appendChild(node('account-menu-name'));
    node('account-menu-identity').appendChild(node('account-menu-username'));
    node('account-menu-logout').appendChild(node('account-menu-logout-label'));
    node('sidebar-version').tagName = 'A';
    node('sidebar-version').setAttribute('href', 'https://www.rsm.global/china/zh-hans');
    for (const id of ['sidebar-account-menu', 'account-menu-identity', 'account-menu-status',
        'account-menu-retry', 'account-menu-logout', 'auth-check-retry', 'login-form',
        'tenant-menu']) node(id).classList.add('hidden');
    node('login-overlay').appendChild(node('auth-check-panel'));
    node('auth-check-panel').appendChild(node('auth-check-message'));
    node('auth-check-panel').appendChild(node('auth-check-retry'));
    const counter = { init: 0, reload: 0, tenant: 0, workspace: 0, session: 0 };
    const ctx = {
        ...eventTarget(), console, Date, URL, URLSearchParams, Headers, Request, AbortController,
        document, currentLang: 'zh', currentView: 'history', activeAgentId: 'agent-a',
        sessionId: 'draft-session', appConfig: {}, brandName: '容大AI',
        innerWidth: 1440, innerHeight: 900,
        location: { reload() { counter.reload++; }, origin: 'http://test', href: 'http://test/chat' },
        sessionStorage: {
            getItem: key => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, String(value)),
            removeItem: key => storage.delete(key),
        },
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        t: key => key, escapeHtml: value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
        effectiveBrandName: () => ctx.brandName,
        effectiveLogoUrl: () => '/logo.svg', effectiveLogoDescription: () => '',
        welcomeHeroDescription: () => '',
        effectiveFaviconUrl: () => '/favicon.ico', brandWordmarkHTML: value => value,
        _brandArmFallback() {}, applyTheme() {}, applyI18n() {}, _applyInputTooltips() {},
        _resetHistorySearch() {}, bumpTenantGeneration() {},
        loadAgentCatalog() {}, readScopedPreference: () => null,
        loadOrCreateSessionId: () => ctx.sessionId, restoreChatState() {}, startPolling() {},
        navigateTo(view) { ctx.currentView = view; },
        refreshWorkspaceSelector() { counter.workspace++; },
        refreshSessionSettings() { counter.session++; },
        requestAnimationFrame: fn => fn(),
        setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout() {},
        getComputedStyle: () => ({ display: 'block', visibility: 'visible' }),
        fetch(url, options) { calls.push({ url, options }); return Promise.resolve().then(() => transport(url, options)); },
    };
    ctx.window = ctx;
    ctx.chatInput = node('chat-input');
    vm.createContext(ctx);
    const run = code => vm.runInContext(code, ctx);
    run("const PRODUCT_NAME = '容大AI'; let APP_VERSION = ''; let _knowledgeTreeData = []; let _knowledgeRootFiles = [];");
    run(section('// Sidebar account state', '// End sidebar account state'));
    if (agentWrapper) run(section('const _nativeFetch = window.fetch.bind(window);', 'function generateSessionId()'));
    run(section('// Authentication\n', '// Initialization\n'));
    const realInitApp = ctx.initApp;
    ctx.initApp = () => { counter.init++; };
    ctx._setupHeaderTenantSelector = () => { counter.tenant++; };
    if (!tenantResolution) ctx._ensureTenantSelected = () => Promise.resolve(true);
    node('login-overlay').classList.add('hidden');
    node('app').classList.add('hidden');
    run(section('// Initialization\n'));
    return {
        ctx, run, node, document, nodes, calls, storage, counter, realInitApp,
        state: () => run('({..._accountState})'),
        flushTimers: async () => { while (timers.length) timers.shift()(); await settle(); },
        login: async (username, password = 'valid-password') => {
            node('login-username').value = username;
            node('login-password').value = password;
            const submitted = node('login-form').onsubmit({ preventDefault() {} });
            await submitted; await settle();
        },
        fetches: url => calls.filter(call => call.url.split('?')[0] === url),
    };
}

async function appearanceSetup(identity = database()) {
    const h = setup(async () => response(identity));
    await settle();
    const localStore = new Map();
    const localStorage = {
        getItem: key => localStore.get(key) ?? null,
        setItem: (key, value) => localStore.set(key, String(value)),
    };
    h.ctx.localStorage = localStorage;
    h.document.documentElement = element(h.document, 'html');
    const dialog = h.node('appearance-dialog');
    h.document.body.appendChild(dialog);
    dialog.open = false;
    dialog.showModal = () => { dialog.open = true; };
    dialog.close = () => { dialog.open = false; dialog.dispatch('close'); };
    const radios = {};
    for (const [name, values] of Object.entries({ 'web-palette': ['business', 'slate', 'classic'],
        'web-mode': ['light', 'dark', 'system'], 'web-language': ['zh', 'zh-Hant', 'en'] })) {
        radios[name] = values.map(value => {
            const input = element(h.document, 'input'); input.value = value; dialog.appendChild(input); return input;
        });
    }
    h.document.querySelectorAll = selector => radios[selector.match(/^input\[name="([^"]+)"\]$/)?.[1]] || [];
    dialog.querySelector = selector => selector.includes('web-palette') ? radios['web-palette'].find(input => input.checked) : null;
    h.node('sidebar-account-menu').appendChild(h.node('account-menu-prefs'));
    h.node('account-menu-prefs').tagName = 'BUTTON';
    h.node('sidebar-account-toggle').isConnected = true;
    h.ctx.CowAppearance = createAppearanceController({ storage: localStorage, root: h.document.documentElement,
        matchMedia: () => ({ matches: false }) });
    h.ctx.rerenderDynamicViews = () => {};
    h.ctx.applyI18n = () => h.ctx.renderAppearancePreferences();
    h.run(section('function setLanguageLocal(', '// Persist the language to the backend'));
    h.run(section('// Theme\n', '// Task completion notification'));
    h.ctx.applyTheme();
    return { ...h, localStore, radios, dialog };
}

test('account preferences open the shared controller, dialog and full persisted selection', async () => {
    const h = await appearanceSetup();
    h.ctx.toggleAccountMenu();
    h.ctx.openAccountPrefs();
    assert.equal(h.dialog.open, true);
    assert.equal(h.node('sidebar-account-menu').classList.contains('hidden'), true);
    h.ctx.setAccountTheme('dark');
    assert.equal(h.ctx.CowAppearance.getState().resolved, 'dark');
    assert.equal(h.ctx.CowAppearance.getState().palette, 'business');
    assert.equal(h.localStore.get('cow_web_palette'), 'business');
    assert.equal(h.radios['web-mode'].find(input => input.checked).value, 'dark');
    h.ctx.closeAccountPrefs();
    assert.equal(h.document.activeElement, h.node('sidebar-account-toggle'));
    // Logout and appearance have exactly one entry each, so reopening the same
    // entry must show the persisted selection instead of a second panel.
    h.ctx.openAppearancePreferences(h.node('sidebar-account-toggle'));
    assert.equal(h.dialog.open, true);
    h.ctx.setAccountTheme('system');
    assert.equal(h.radios['web-mode'].find(input => input.checked).value, 'system');
    h.ctx.closeAppearancePreferences();
    assert.equal(h.document.activeElement, h.node('sidebar-account-toggle'));
    const restored = createAppearanceController({ storage: h.ctx.localStorage });
    assert.equal(restored.getState().palette, 'business');
    assert.equal(restored.getState().mode, 'system');
});

test('unified language selection is local and its storage warning is independent of appearance reset', async () => {
    const h = await appearanceSetup();
    h.ctx.openAccountPrefs();
    const callsBefore = h.calls.length;
    h.ctx.setAccountLang('en');
    assert.equal(h.ctx.currentLang, 'en');
    assert.equal(h.localStore.get('cow_lang'), 'en');
    assert.equal(h.radios['web-language'].find(input => input.checked).value, 'en');
    assert.equal(h.calls.length, callsBefore, 'personal language must not write instance config');
    const setItem = h.ctx.localStorage.setItem;
    h.ctx.localStorage.setItem = (key, value) => {
        if (key === 'cow_lang') throw Error('storage blocked');
        setItem(key, value);
    };
    h.ctx.setAccountLang('zh-Hant');
    assert.equal(h.ctx.currentLang, 'zh-Hant');
    assert.equal(h.node('appearance-language-warning').hidden, false);
    h.ctx.CowAppearance.reset();
    assert.equal(h.node('appearance-language-warning').hidden, false);
    assert.equal(h.node('appearance-storage-warning').hidden, true);
    assert.equal(h.ctx.currentLang, 'zh-Hant');
});

test('preferences close cleanly at login and remain available in legacy and public modes', async () => {
    const h = await appearanceSetup();
    h.ctx.openAccountPrefs();
    h.ctx.showLoginScreen();
    assert.equal(h.dialog.open, false);
    assert.equal(h.run('_activeAccountPanel'), null);
    assert.equal(h.document.activeElement, h.node('login-username'));
    for (const auth_required of [true, false]) {
        const legacy = await appearanceSetup({ status: 'success', auth_required, authenticated: auth_required });
        assert.equal(legacy.node('account-menu-prefs').classList.contains('hidden'), false);
        legacy.ctx.openAccountPrefs();
        assert.equal(legacy.dialog.open, true);
    }
});

test('opening preferences respects the existing password dismissal guard', async () => {
    const h = await appearanceSetup();
    h.run('_setAccountPanel("password");');
    h.node('ap-old-password').value = 'keep-input';
    h.node('ap-old-password').dataset.dirty = '1';
    h.ctx.confirm = () => false;
    h.ctx.openAccountPrefs();
    assert.equal(h.dialog.open, false);
    assert.equal(h.node('ap-old-password').value, 'keep-input');
    assert.equal(h.run('_activeAccountPanel'), 'password');
    h.ctx.confirm = () => true;
    h.ctx.openAccountPrefs();
    assert.equal(h.dialog.open, true);
    assert.equal(h.node('account-password-modal').classList.contains('hidden'), true);
    h.ctx.closeAccountPrefs();
    h.run('_forcedPassword = true;');
    h.ctx.openAccountPrefs();
    assert.equal(h.dialog.open, false, 'preferences cannot cover the forced-password gate');
});

test('tenant entry loads memberships without opening the profile and preserves browser preferences', async () => {
    const h = setup(async url => url === '/auth/me'
        ? response({ status: 'success', user: { username: 'alice' }, tenants: [{ id: 'a' }, { id: 'b' }] })
        : response(database()));
    await settle();
    h.storage.set('cow_tenant_id', 'a');
    h.ctx.prompt = () => 'not-a-number';
    h.ctx.alert = () => {};
    let destination = '';
    h.ctx.location.assign = value => { destination = value; };
    await h.ctx.openAccountTenant();
    assert.equal(destination, '');
    h.ctx.prompt = () => '2';
    await h.ctx.openAccountTenant();
    assert.ok(h.fetches('/auth/me').length >= 1);
    assert.equal(new URL(destination).searchParams.get('switch_tenant'), 'b');
    assert.equal(h.storage.get('cow_tenant_id'), 'a', 'old page must not pre-commit the next tenant');
});

test('startup check failure stays neutral; a visible retry enters database mode and initializes only once', async () => {
    let requests = 0;
    const h = setup(async () => {
        if (++requests === 1) throw Error('offline');
        return response(database());
    });
    await settle();
    assert.equal(h.counter.init, 0);
    assert.equal(h.node('login-overlay').classList.contains('hidden'), false);
    assert.equal(h.node('login-form').classList.contains('hidden'), true);
    assert.equal(h.node('auth-check-retry').classList.contains('hidden'), false);
    assert.equal(h.state().mode, 'unknown');
    assert.equal(h.state().username, '');
    await h.ctx.refreshAccountIdentity();
    assert.equal(h.state().username, 'alice');
    await settle();
    assert.equal(h.counter.init, 1);
    assert.equal(h.node('login-overlay').classList.contains('hidden'), true);
    await h.ctx.refreshAccountIdentity();
    await settle();
    assert.equal(h.counter.init, 1);
});

test('database display is plain text, preserves the name, and falls back to username for whitespace names', async () => {
    let user = { username: '<alice>', display_name: '  <img src=x onerror=alert(1)>  ' };
    const h = setup(async () => response(database('', { user })));
    await settle();
    assert.equal(h.state().username, '<alice>');
    assert.equal(h.state().displayName, user.display_name);
    assert.equal(h.node('sidebar-account-name').textContent, user.display_name);
    assert.equal(h.node('sidebar-account-name').innerHTML, '');
    assert.equal(h.node('sidebar-account-subtitle').textContent, '@<alice>');
    assert.equal(h.node('account-menu-name').textContent, user.display_name);
    user = { username: 'fallback', display_name: '  ' };
    await h.ctx.refreshAccountIdentity();
    assert.equal(h.state().username, 'fallback');
    assert.equal(h.node('sidebar-account-name').textContent, 'fallback');
});

test('legacy responses distinguish password protection and explicit anonymous access', async () => {
    let payload = { status: 'success', auth_required: true, authenticated: true };
    const h = setup(async () => response(payload));
    await settle();
    assert.equal(h.state().mode, 'legacy');
    assert.equal(h.state().authRequired, true);
    assert.equal(h.node('account-menu-logout').classList.contains('hidden'), false);
    payload = { status: 'success', auth_required: false };
    await h.ctx.refreshAccountIdentity();
    assert.equal(h.state().mode, 'legacy');
    assert.equal(h.state().authRequired, false);
    assert.equal(h.state().username, '');
    assert.equal(h.node('account-menu-logout').classList.contains('hidden'), true);
});

test('HTTP, business and incomplete check responses cannot masquerade as anonymous access', async () => {
    for (const invalid of [
        response(database(), 503), response({ status: 'error', auth_required: false }),
        response({}), response({ status: 'success', identity_mode: 'database', auth_required: true }),
        response(null),
    ]) {
        const h = setup(async () => invalid);
        await settle();
        assert.equal(h.counter.init, 0);
        assert.equal(h.state().mode, 'unknown');
        assert.equal(h.state().username, '');
        assert.equal(h.node('login-overlay').classList.contains('hidden'), false);
    }
});

test('account retry sends no tenant header, preserves draft and focus, and does not initialize business data', async () => {
    const pending = deferred(); let checks = 0;
    const h = setup(() => ++checks === 1 ? response(database()) : pending.promise);
    await settle();
    h.storage.set('cow_tenant_id', 'expired-tenant');
    h.node('chat-input').value = 'An unsaved draft';
    h.node('chat-input').focus();
    const active = h.document.activeElement;
    const first = h.ctx.refreshAccountIdentity();
    const repeated = h.ctx.refreshAccountIdentity();
    await settle();
    assert.equal(h.fetches('/auth/check').length, 2);
    const opts = h.fetches('/auth/check').at(-1).options;
    assert.equal(new Headers(opts?.headers).has('X-Tenant-ID'), false);
    pending.resolve(response(database('alice')));
    await first; await repeated;
    await settle();
    assert.equal(h.counter.init, 1);
    assert.equal(h.counter.workspace, 0);
    assert.equal(h.counter.session, 0);
    assert.equal(h.fetches('/api/version').length, 0);
    assert.equal(h.storage.get('cow_tenant_id'), 'expired-tenant');
    assert.equal(h.ctx.currentView, 'history');
    assert.equal(h.ctx.activeAgentId, 'agent-a');
    assert.equal(h.ctx.sessionId, 'draft-session');
    assert.equal(h.node('chat-input').value, 'An unsaved draft');
    assert.equal(h.document.activeElement, active);
});

test('authenticated database response without user still permits logout and never shows an old name', async () => {
    let missing = false;
    const h = setup(async url => url === '/auth/logout' ? response({ status: 'success' })
        : response(database('alice', missing ? { user: {} } : {})));
    await settle();
    missing = true;
    await h.ctx.refreshAccountIdentity();
    assert.equal(h.state().authenticated, true);
    assert.equal(h.state().username, '');
    assert.equal(h.node('account-menu-logout').classList.contains('hidden'), false);
    assert.equal(h.node('account-menu-retry').classList.contains('hidden'), false);
    await h.ctx.handleLogout(); await settle();
    assert.equal(h.fetches('/auth/logout').length, 1);
    assert.equal(h.counter.reload, 1);
});

test('multi-tenant login preserves identity; expiry clears the old picker so a new account submits normally', async () => {
    const h = setup(async (url, options) => {
        if (url === '/auth/check') return response(database('', { authenticated: false, user: undefined }));
        if (url === '/api/protected') return response({}, 401);
        const username = JSON.parse(options.body).username;
        return response({ ...database(username), tenants: username === 'alice'
            ? [{ id: 'one', name: 'One' }, { id: 'two', name: 'Two' }]
            : username === 'charlie' ? [{ id: 'three', name: 'Three' }] : [] });
    });
    await settle();
    await h.login('alice');
    assert.equal(h.counter.init, 0);
    assert.equal(h.state().username, 'alice');
    assert.equal(typeof h.node('login-btn').onclick, 'function');
    h.node('login-tenant-select').value = 'two';
    h.node('login-btn').onclick({ preventDefault() {} });
    await settle();
    assert.equal(h.storage.get('cow_tenant_id'), 'two');
    assert.equal(h.state().username, 'alice');
    assert.equal(h.counter.init, 1);
    await h.ctx.fetch('/api/protected');
    assert.equal(h.node('login-btn').onclick, null);
    assert.equal(h.node('login-tenant-select').children.length, 0);
    assert.equal(h.state().username, '');
    await h.login('bob');
    assert.equal(h.fetches('/auth/login').length, 2);
    assert.equal(JSON.parse(h.fetches('/auth/login')[1].options.body).username, 'bob');
    assert.equal(h.state().username, 'bob');
    assert.equal(h.node('login-overlay').classList.contains('hidden'), true);
    assert.equal(h.storage.has('cow_tenant_id'), false);
    await h.ctx.fetch('/api/protected');
    await h.login('charlie');
    assert.equal(h.state().username, 'charlie');
    assert.equal(h.storage.get('cow_tenant_id'), 'three');
    assert.equal(h.node('login-tenant-group').classList.contains('hidden'), true);
});

test('legacy login normalizes the status-only success contract and serializes repeated submissions', async () => {
    const pending = deferred();
    const h = setup(async url => url === '/auth/check'
        ? response({ status: 'success', auth_required: true, authenticated: false }) : pending.promise);
    await settle();
    const first = h.login('', 'shared-password');
    const second = h.login('', 'shared-password');
    await settle();
    assert.equal(h.fetches('/auth/login').length, 1);
    assert.deepEqual(JSON.parse(h.fetches('/auth/login')[0].options.body), { password: 'shared-password' });
    pending.resolve(response({ status: 'success' }));
    await first; await second;
    assert.equal(h.state().mode, 'legacy');
    assert.equal(h.state().authenticated, true);
    assert.equal(h.counter.init, 1);
});

for (const outcome of ['success', 'failure', '401']) {
    test(`a late account A check ${outcome} cannot restore A or log account B out`, async () => {
        const stale = deferred(); let checks = 0;
        const h = setup(async (url, options) => {
            if (url === '/auth/login') return response({ ...database(JSON.parse(options.body).username), tenants: [] });
            if (url === '/api/expire') return response({}, 401);
            return ++checks === 1 ? response(database('alice')) : stale.promise;
        });
        await settle();
        const oldCheck = h.ctx.refreshAccountIdentity();
        await settle();
        await h.ctx.fetch('/api/expire');
        await h.login('bob');
        const initialized = h.counter.init;
        if (outcome === 'failure') stale.reject(Error('old request failed'));
        else stale.resolve(outcome === '401' ? response({}, 401) : response(database('alice')));
        await oldCheck; await settle();
        assert.equal(h.state().username, 'bob');
        assert.equal(h.node('login-overlay').classList.contains('hidden'), true);
        assert.equal(h.counter.init, initialized);
    });
}

test('old business 401 returns unchanged without logging B out; same-identity 403 also preserves B', async () => {
    const stale = deferred();
    const h = setup(async (url, options) => {
        url = url.split('?')[0];
        if (url === '/auth/login') return response({ ...database(JSON.parse(options.body).username), tenants: [] });
        if (url === '/api/old') return stale.promise;
        if (url === '/api/expire') return response({}, 401);
        if (url === '/api/forbidden') return response({}, 403);
        return response(database('alice'));
    }, { agentWrapper: true });
    await settle();
    const oldRequest = h.ctx.fetch('/api/old');
    assert.equal(h.fetches('/api/old')[0].url, '/api/old?agent_id=agent-a');
    await h.ctx.fetch('/api/expire');
    await h.login('bob');
    const original = response({}, 401);
    stale.resolve(original);
    assert.equal(await oldRequest, original);
    assert.equal(h.state().username, 'bob');
    await h.ctx.fetch('/api/forbidden');
    assert.equal(h.state().username, 'bob');
    assert.equal(h.node('login-overlay').classList.contains('hidden'), true);
});

test('current business 401 survives an ordinary retry, including confirmed login with temporarily missing user data', async () => {
    for (const checkFinished of [false, true]) {
        const checking = deferred(), business = deferred(); let checks = 0;
        const h = setup(async url => url === '/api/protected' ? business.promise
            : ++checks === 1 ? response(database()) : checking.promise);
        await settle();
        const protectedRequest = h.ctx.fetch('/api/protected');
        const retry = h.ctx.refreshAccountIdentity();
        await settle();
        if (checkFinished) {
            checking.resolve(response(database('', { user: {} })));
            await retry;
            assert.equal(h.state().authenticated, true);
        }
        business.resolve(response({}, 401));
        await protectedRequest;
        assert.equal(h.node('login-overlay').classList.contains('hidden'), false);
        assert.equal(h.state().username, '');
        if (!checkFinished) {
            checking.resolve(response(database()));
            await retry;
        }
        assert.equal(h.node('login-overlay').classList.contains('hidden'), false);
        assert.equal(h.state().username, '');
    }
});

test('logout stays single-flight on the single main entry; a business error provides recovery and retry can succeed', async () => {
    const pending = deferred(); let logouts = 0;
    const h = setup(async url => url === '/auth/logout'
        ? (++logouts === 1 ? pending.promise : response({ status: 'success' })) : response(database()));
    await settle();
    const first = h.ctx.handleLogout();
    const second = h.ctx.handleLogout();
    await settle();
    assert.equal(h.fetches('/auth/logout').length, 1);
    assert.equal(h.state().username, '');
    assert.equal(h.node('account-menu-logout').disabled, true);
    pending.resolve(response({ status: 'error', message: 'Logout was not completed' }));
    await first; await second; await settle();
    assert.equal(h.counter.reload, 0);
    assert.equal(h.node('account-menu-logout').disabled, false);
    assert.equal(h.node('account-menu-retry').classList.contains('hidden'), false);
    assert.equal(h.node('account-menu-logout').classList.contains('hidden'), false);
    await h.ctx.handleLogout(); await settle();
    assert.equal(h.fetches('/auth/logout').length, 2);
    assert.equal(h.counter.reload, 1);
});

test('network, HTTP and JSON logout failures may be rechecked to recover a still-authenticated account', async () => {
    for (const failure of [
        () => { throw Error('connection lost'); },
        () => response({ status: 'success' }, 503),
        () => ({ ok: true, status: 200, json: async () => { throw Error('Invalid JSON'); } }),
    ]) {
        const h = setup(async url => url === '/auth/logout' ? failure() : response(database()));
        await settle();
        await h.ctx.handleLogout(); await settle();
        assert.equal(h.state().username, '');
        assert.equal(h.counter.reload, 0);
        const initialized = h.counter.init;
        h.ctx.toggleAccountMenu();
        h.node('account-menu-retry').focus();
        await h.ctx.refreshAccountIdentity();
        assert.equal(h.state().username, 'alice');
        assert.equal(h.counter.init, initialized);
        assert.equal(h.document.activeElement, h.node('sidebar-version'));
    }
});

test('valid version survives invalid responses, and a brand repaint changes only the version label', async () => {
    let versionResponse = response({ status: 'success' });
    const h = setup(async url => url === '/api/version' ? versionResponse
        : url === '/auth/check' ? response(database()) : response({ status: 'success' }));
    await settle();
    h.realInitApp(); await settle();
    assert.equal(h.node('sidebar-version').textContent, '容大AI');
    versionResponse = response({ status: 'success', version: '2.1.7' });
    h.realInitApp(); await settle();
    assert.match(h.node('sidebar-version').textContent, /2\.1\.7/);
    for (const invalid of [response({ status: 'success' }), response({ version: '999' }, 503), response({ status: 'error', version: '999' })]) {
        versionResponse = invalid;
        h.realInitApp(); await settle();
        assert.match(h.node('sidebar-version').textContent, /2\.1\.7/);
        assert.doesNotMatch(h.node('sidebar-version').textContent, /undefined|999/);
    }
    const before = h.state().username;
    const focus = h.node('account-menu-logout'); focus.focus();
    h.ctx.brandName = 'Published Brand';
    h.run(section('function applyBrandToDocument()', 'function applyBrandToAgentAvatars()'));
    h.ctx.applyBrandToDocument();
    assert.equal(h.node('sidebar-version').textContent, 'Published Brand v2.1.7');
    assert.equal(h.state().username, before);
    assert.equal(h.document.activeElement, focus);
});

// --- Integrated same-origin help entry ----------------------------------
function collectOpened(h) {
    const opened = [];
    h.ctx.open = (url, target) => { opened.push([url, target]); };
    return opened;
}

function runFetchPublicBrand(h) {
    h.run("let brandFetchSeq = 0; let brandSaveEpoch = 0;");
    h.ctx.applyBrandToDocument = () => {};
    h.ctx.applyBrandToAgentAvatars = () => {};
    h.run(section('function fetchPublicBrand(seq)', '// Fetch immediately'));
}

test('帮助与关于 opens the project site address, not the brand version row', async () => {
    const h = setup();
    await settle();
    const opened = collectOpened(h);
    h.ctx.openAccountAbout();
    assert.deepEqual(opened, [['/help/', '_blank']]);
    // The version row keeps its own target, and replacing it does not move the
    // help entry: the two are no longer the same link.
    assert.equal(h.node('sidebar-version').getAttribute('href'),
        'https://www.rsm.global/china/zh-hans');
    h.node('sidebar-version').setAttribute('href', 'https://stale.invalid/');
    h.ctx.openAccountAbout();
    assert.equal(opened.length, 2);
    assert.equal(opened[1][0], '/help/');
});

test('the public brand snapshot cannot redirect integrated help off-site', async () => {
    const h = setup(async url => url === '/api/branding/public'
        ? response({ enabled: true, revision: 2, help_url: 'https://help.example.com/webhelp' })
        : response({ status: 'success' }));
    await settle();
    runFetchPublicBrand(h);
    await h.ctx.fetchPublicBrand();
    assert.equal(h.run('_accountAboutUrl'), '/help/');
    const opened = collectOpened(h);
    h.ctx.openAccountAbout();
    assert.deepEqual(opened, [['/help/', '_blank']]);
});

test('an absent or unusable help_url keeps integrated help available', async () => {
    for (const payload of [
        { enabled: true, revision: 1 },                       // snapshot without the field
        { enabled: true, revision: 1, help_url: '' },
        { enabled: true, revision: 1, help_url: 'javascript:alert(1)' },
        { enabled: true, revision: 1, help_url: 'localhost:8080' },
        { enabled: true, revision: 1, help_url: 'not a url' },
    ]) {
        const h = setup(async url => url === '/api/branding/public'
            ? response(payload) : response({ status: 'success' }));
        await settle();
        runFetchPublicBrand(h);
        await h.ctx.fetchPublicBrand();
        assert.equal(h.run('_accountAboutUrl'), '/help/',
            JSON.stringify(payload));
        const opened = collectOpened(h);
        h.ctx.openAccountAbout();
        assert.deepEqual(opened, [['/help/', '_blank']]);
    }
});

test('a failed public brand read leaves the help target usable', async () => {
    const h = setup(async url => {
        if (url === '/api/branding/public') throw Error('brand read failed');
        return response({ status: 'success' });
    });
    await settle();
    runFetchPublicBrand(h);
    await h.ctx.fetchPublicBrand();
    const opened = collectOpened(h);
    h.ctx.openAccountAbout();
    assert.deepEqual(opened, [['/help/', '_blank']]);
});

test('account menu restores focus on Escape and closes on outside pointer without taking the new focus', async () => {
    const h = setup();
    await settle();
    h.ctx.toggleAccountMenu();
    await h.flushTimers();
    assert.equal(h.node('sidebar-account-menu').classList.contains('hidden'), false);
    assert.equal(h.node('sidebar-account-toggle').getAttribute('aria-expanded'), 'true');
    assert.equal(h.document.activeElement, h.node('account-menu-logout'));
    h.document.dispatch('keydown', { key: 'Escape', target: h.document.activeElement });
    await h.flushTimers();
    assert.equal(h.node('sidebar-account-menu').classList.contains('hidden'), true);
    assert.equal(h.node('sidebar-account-toggle').getAttribute('aria-expanded'), 'false');
    assert.equal(h.document.activeElement, h.node('sidebar-account-toggle'));
    h.ctx.toggleAccountMenu();
    const outside = h.node('chat-input');
    outside.focus();
    h.document.dispatch('pointerdown', { target: outside });
    assert.equal(h.node('sidebar-account-menu').classList.contains('hidden'), true);
    assert.equal(h.document.activeElement, outside);
    h.ctx.toggleAccountMenu();
    outside.focus();
    h.document.dispatch('focusin', { target: outside });
    assert.equal(h.node('sidebar-account-menu').classList.contains('hidden'), true);
    assert.equal(h.document.activeElement, outside, 'Tab leaving the account region must keep the destination focus');
});

test('account and tenant menus are mutually exclusive and repeated opening does not accumulate listeners', async () => {
    const h = setup();
    await settle();
    h.node('tenant-menu').classList.remove('hidden');
    h.ctx.toggleAccountMenu();
    assert.equal(h.node('tenant-menu').classList.contains('hidden'), true);
    const listeners = h.document.listenerCount('pointerdown');
    h.ctx.toggleTenantMenu({ stopPropagation() {} });
    assert.equal(h.node('tenant-menu').classList.contains('hidden'), false);
    assert.equal(h.node('sidebar-account-menu').classList.contains('hidden'), true);
    for (let i = 0; i < 3; i++) {
        h.ctx.toggleAccountMenu();
        h.ctx.closeAccountMenu();
    }
    h.ctx.toggleAccountMenu();
    assert.equal(h.document.listenerCount('pointerdown'), listeners);
});

test('the mobile account panel is a modal bottom sheet outside the transformed sidebar', async () => {
    const h = setup();
    await settle();
    const menu = h.node('sidebar-account-menu');
    h.ctx.innerWidth = 390;
    h.ctx.innerHeight = 700;
    h.ctx.toggleAccountMenu();
    assert.equal(menu.parentNode, h.document.body, 'hosted outside the off-canvas sidebar');
    assert.equal(menu.classList.contains('account-menu-sheet'), true);
    assert.equal(menu.getAttribute('role'), 'dialog');
    assert.equal(menu.getAttribute('aria-modal'), 'true');
    assert.equal(h.node('account-menu-sheet-head').classList.contains('hidden'), false);
    assert.equal(h.node('account-menu-sheet-close').classList.contains('hidden'), false);
    assert.equal(h.node('account-menu-backdrop').classList.contains('hidden'), false);
    assert.equal(h.document.body.classList.contains('account-menu-sheet-open'), true);

    // Tab cycles inside the sheet instead of reaching the page behind it.
    const focusable = h.ctx._accountMenuFocusable(menu);
    assert.ok(focusable.length >= 2, 'the sheet has more than one stop');
    focusable[focusable.length - 1].focus();
    h.document.dispatch('keydown', { key: 'Tab', shiftKey: false, target: focusable[focusable.length - 1] });
    assert.equal(h.document.activeElement, focusable[0], 'Tab wraps to the first stop');
    h.document.dispatch('keydown', { key: 'Tab', shiftKey: true, target: focusable[0] });
    assert.equal(h.document.activeElement, focusable[focusable.length - 1], 'Shift+Tab wraps back');

    // Closing puts the single panel node back on the desktop host and drops the
    // chrome with it.
    h.ctx.closeAccountMenu();
    assert.equal(menu.parentNode, h.node('sidebar-account-footer'));
    assert.equal(menu.classList.contains('account-menu-sheet'), false);
    assert.equal(menu.getAttribute('role'), 'group');
    assert.equal(h.node('account-menu-backdrop').classList.contains('hidden'), true);
    assert.equal(h.document.body.classList.contains('account-menu-sheet-open'), false);
});

// The account card's own 账号设置 group: the retained group whose actions the
// focus-order test drives. (The 「我的资源」 group this used to mount is retired by
// change unify-console-by-data-scope, task 3.3.)
function mountAccountSettings(h) {
    const group = h.node('account-menu-settings');
    const entry = h.node('account-menu-profile');
    // The panel's focusable set is `button, a`; the lazy harness node for this id
    // is a div, so name the button the real markup ships.
    entry.tagName = 'BUTTON';
    group.appendChild(entry);
    h.node('sidebar-account-menu').appendChild(group);
    return { group, byView: id => h.node(id) };
}

test('an entry inside a hidden group is not focusable, and the panel is sized to the room it has', async () => {
    const h = setup();
    await settle();
    const menu = h.node('sidebar-account-menu');
    const { group, byView } = mountAccountSettings(h);
    h.ctx.toggleAccountMenu();
    const focusable = () => h.ctx._accountMenuFocusable(menu);
    byView('account-menu-profile').classList.remove('hidden');
    group.classList.add('hidden');
    assert.ok(!focusable().includes(byView('account-menu-profile')),
        'an entry inside a hidden group is not focusable');
    group.classList.remove('hidden');
    assert.ok(focusable().includes(byView('account-menu-profile')), 'a visible entry joins the order');

    // The popover is capped by the space between the account card and the top of
    // the viewport, so the last entry stays reachable in a short window.
    const footer = h.node('sidebar-account-footer');
    const footerRect = footer.getBoundingClientRect;
    footer.getBoundingClientRect = () => ({ top: 120, left: 0, width: 224, height: 72, bottom: 192 });
    h.ctx._applyAccountMenuHeight();
    assert.equal(menu.style.maxHeight, '108px');
    footer.getBoundingClientRect = footerRect;
    h.ctx._applyAccountMenuHeight();
    assert.equal(menu.style.maxHeight, '768px', 'the default popover clears the account card');
    h.node('app').classList.add('sidebar-collapsed');
    h.ctx.innerHeight = 900;
    h.ctx._applyAccountMenuHeight();
    assert.equal(menu.style.maxHeight, '876px', 'the icon-sidebar panel uses the viewport');
    h.node('app').classList.remove('sidebar-collapsed');
    // Below the sidebar breakpoint the panel is remounted as a sheet, whose size
    // is bounded by CSS (80% of the visual viewport) instead of an inline value.
    h.ctx.closeAccountMenu();
    h.ctx.innerWidth = 390;
    h.ctx.toggleAccountMenu();
    h.ctx._applyAccountMenuHeight();
    assert.equal(menu.style.maxHeight, '', 'the sheet is bounded by CSS');
});

test('forced password login shows the change gate and never starts tenant/business init', async () => {
    const h = setup(async () => response(database('', { must_change_password: true })));
    await settle();
    await h.ctx.refreshAccountIdentity();
    await settle();
    assert.equal(h.state().username, '');
    assert.equal(h.state().mustChangePassword, true);
    assert.equal(h.counter.init, 0, 'forced gate must not initialize business data');
    assert.equal(h.run('_forcedPassword'), true);
    // The login overlay stays as backdrop and the password gate is opened.
    assert.equal(h.node('login-overlay').classList.contains('hidden'), false);
    assert.equal(h.node('account-password-modal').classList.contains('hidden'), false);
    // The gate cannot be dismissed by closing the modal.
    h.ctx.closeAccountPassword();
    assert.equal(h.node('account-password-modal').classList.contains('hidden'), false);
});

test('forced password login through the form also enters the gate', async () => {
    const h = setup(async (url) => {
        if (url === '/auth/check') return response(database('', { authenticated: false, user: undefined }));
        return response({ ...database('alice', { must_change_password: true }), tenants: [{ id: 't', name: 'T' }] });
    });
    await settle();
    await h.ctx.refreshAccountIdentity();
    // /auth/check says unauthenticated, then login with a restricted account.
    await h.login('alice');
    await settle();
    assert.equal(h.state().username, 'alice');
    assert.equal(h.state().mustChangePassword, true);
    assert.equal(h.counter.init, 0);
    assert.equal(h.run('_forcedPassword'), true);
    assert.equal(h.node('account-password-modal').classList.contains('hidden'), false);
    // No tenant was committed: business load must not start on a forced account.
    assert.equal(h.storage.has('cow_tenant_id'), false);
});

test('closing a forced gate is blocked, but logout is the only way out and resets the gate', async () => {
    const h = setup(async (url) => {
        if (url === '/auth/logout') return response({ status: 'success' });
        return response(database('', { must_change_password: true }));
    });
    await settle();
    await h.ctx.refreshAccountIdentity();
    await settle();
    assert.equal(h.run('_forcedPassword'), true);
    // Dismiss attempts are ignored.
    h.ctx.closeAccountPassword();
    assert.equal(h.run('_forcedPassword'), true);
    assert.equal(h.node('account-password-modal').classList.contains('hidden'), false);
    // Logout resets the forced flag and reloads.
    await h.ctx.handleLogout();
    await settle();
    assert.equal(h.counter.reload, 1);
    assert.equal(h.run('_forcedPassword'), false);
});

test('ordinary password change success does not trigger the gate and clears forced state', async () => {
    const h = setup(async (url, options) => {
        if (url === '/auth/password') {
            assert.equal(JSON.parse(options.body).old_password, 'old-pw');
            return response({ status: 'success', must_relogin: true });
        }
        return response(database('alice'));
    });
    await settle();
    await h.ctx.refreshAccountIdentity();
    await settle();
    assert.equal(h.counter.init, 1);
    assert.equal(h.run('_forcedPassword'), false);
    h.node('ap-old-password').value = 'old-pw';
    h.node('ap-new-password').value = 'new-pw';
    h.node('ap-confirm-password').value = 'new-pw';
    await h.ctx.submitAccountPassword({ preventDefault() {} });
    await settle();
    await h.flushTimers();
    assert.equal(h.state().username, '');
    assert.equal(h.run('_forcedPassword'), false);
    // Back to the login screen after a successful change (session revoked).
    assert.equal(h.node('login-overlay').classList.contains('hidden'), false);
});


test('reload selects the only effective membership even when a platform admin can list other tenants', async () => {
    const membership = deferred();
    const h = setup(async url => {
        url = url.split('?')[0];
        if (url === '/auth/check') return response(database('admin'));
        if (url === '/auth/me') return membership.promise;
        if (url === '/api/platform/tenants') return response({ items: [{ id: 'default' }, { id: 'ztest-aaa' }] });
        return response({ status: 'success' });
    }, { agentWrapper: true, tenantResolution: true });
    await settle();
    assert.equal(h.counter.init, 0);
    assert.equal(h.node('app').classList.contains('hidden'), true);
    membership.resolve(response({ status: 'success', tenants: [{ id: 'default', name: 'Default' }] }));
    await settle();
    assert.equal(h.storage.get('cow_tenant_id'), 'default');
    assert.equal(h.counter.init, 1);
    assert.equal(h.counter.tenant, 1);
    assert.equal(h.fetches('/api/platform/tenants').length, 0);
    await h.ctx.fetch('/message', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: 'hello' }) });
    const sent = h.fetches('/message')[0];
    assert.equal(new Headers(sent.options.headers).get('X-Tenant-ID'), 'default');
    assert.equal(JSON.parse(sent.options.body).agent_id, 'agent-a');
});

test('an ordinary member restores chat without permission to list platform tenants', async () => {
    const h = setup(async url => {
        if (url === '/auth/check') return response(database('member'));
        if (url === '/auth/me') return response({ status: 'success', tenants: [{ id: 'team' }] });
        return response({ status: 'error', message: 'forbidden' }, 403);
    }, { tenantResolution: true });
    await settle();
    assert.equal(h.storage.get('cow_tenant_id'), 'team');
    assert.equal(h.counter.init, 1);
    assert.equal(h.fetches('/api/platform/tenants').length, 0);
});

test('reload with multiple memberships waits for a visible picker, then initializes once', async () => {
    const h = setup(async url => url === '/auth/check' ? response(database())
        : response({ status: 'success', tenants: [{ id: 'one', name: 'One' }, { id: 'two', name: 'Two' }] }),
    { tenantResolution: true });
    h.storage.set('cow_tenant_id', 'removed-tenant');
    await settle();
    assert.equal(h.storage.has('cow_tenant_id'), false);
    assert.equal(h.counter.init, 0);
    assert.equal(h.node('app').classList.contains('hidden'), true);
    assert.equal(h.node('login-form').classList.contains('hidden'), false);
    assert.equal(h.node('auth-check-panel').classList.contains('hidden'), true);
    assert.equal(h.node('login-tenant-select').children.length, 2);
    h.node('login-tenant-select').value = 'two';
    h.node('login-btn').onclick({ preventDefault() {} });
    await settle();
    assert.equal(h.storage.get('cow_tenant_id'), 'two');
    assert.equal(h.counter.init, 1);
    assert.equal(h.counter.tenant, 1);
    assert.equal(h.node('login-overlay').classList.contains('hidden'), true);
    await h.ctx.refreshAccountIdentity();
    await settle();
    assert.equal(h.counter.init, 1);
});

test('a stored tenant is validated against current memberships before any business initialization', async () => {
    const pending = deferred();
    const h = setup(async url => url === '/auth/check' ? response(database()) : pending.promise,
        { tenantResolution: true });
    h.storage.set('cow_tenant_id', 'old');
    await settle();
    assert.equal(h.counter.init, 0);
    pending.resolve(response({ status: 'success', tenants: [{ id: 'new' }] }));
    await settle();
    assert.equal(h.storage.get('cow_tenant_id'), 'new');
    assert.equal(h.counter.init, 1);
});

test('membership lookup failure and no memberships keep chat gated with a working retry', async () => {
    for (const unavailable of [response({ status: 'error' }, 503), response({ status: 'success', tenants: [] })]) {
        let ready = false;
        const h = setup(async url => url === '/auth/check' ? response(database())
            : ready ? response({ status: 'success', tenants: [{ id: 'team' }] }) : unavailable,
        { tenantResolution: true });
        await settle();
        assert.equal(h.counter.init, 0);
        assert.equal(h.node('app').classList.contains('hidden'), true);
        assert.equal(h.node('auth-check-retry').classList.contains('hidden'), false);
        ready = true;
        await h.ctx.refreshAccountIdentity();
        await settle();
        assert.equal(h.counter.init, 1);
        assert.equal(h.storage.get('cow_tenant_id'), 'team');
    }
});

test('a late membership result cannot restore the tenant or app after session expiry', async () => {
    const pending = deferred();
    const h = setup(async url => url === '/auth/check' ? response(database())
        : url === '/auth/me' ? pending.promise : response({}, 401), { tenantResolution: true });
    await settle();
    await h.ctx.fetch('/api/expire');
    pending.resolve(response({ status: 'success', tenants: [{ id: 'old' }] }));
    await settle();
    assert.equal(h.storage.has('cow_tenant_id'), false);
    assert.equal(h.counter.init, 0);
    assert.equal(h.state().authenticated, false);
    assert.equal(h.node('login-form').classList.contains('hidden'), false);
});

function setupSend(transport) {
    const rendered = [], requests = [], retries = [];
    const ctx = {
        Date, console, currentLang: 'zh', sessionId: 'session',
        _identityMode: () => 'legacy', sessionStorage: { getItem: () => null },
        chatInput: { value: 'hello' }, pendingAttachments: [], inputHistory: [],
        historyIdx: -1, historySavedDraft: '', sendBtn: { disabled: false },
        document: { getElementById: () => null },
        t: key => key,
        syncTeamFromText() {}, renderComposerIdentity() {}, resetComposerHeight() {},
        addUserMessage() {}, renderAttachmentPreview() {}, addressedAgentId: () => '',
        addLoadingIndicator: () => ({ remove() {} }), resetSendBtnSendMode() { ctx.sendBtn.disabled = false; },
        createBotMessageEl(initialText) {
            assert.equal(initialText, '');
            const content = { textContent: '', dataset: {}, innerHTML: '' };
            return { content, querySelector: () => content };
        },
        messagesDiv: { appendChild(el) { rendered.push(el); } }, scrollChatToBottom() {},
        setTimeout(fn) { retries.push(fn); },
        fetch(url, options) { requests.push({ url, options }); return Promise.resolve(transport()); },
    };
    vm.createContext(ctx);
    vm.runInContext(section('// Preserve actionable server failures', 'function startSSE('), ctx);
    return { ctx, rendered, requests, retries };
}

test('send errors preserve actionable HTTP failure messages and never retry a rejection', async () => {
    for (const [payload, status, expected] of [
        [{ status: 'error' }, 401, 'error_login_required'],
        [{ status: 'error', message: 'tenant selection required' }, 400, 'error_tenant_required'],
        [{ status: 'error', code: 'missing_tenant' }, 400, 'error_tenant_required'],
        [{ status: 'error', message: 'forbidden' }, 403, 'error_chat_forbidden'],
        [{ status: 'error', code: 'password_change_required' }, 403, 'error_password_required'],
    ]) {
        const h = setupSend(() => response(payload, status));
        h.ctx.sendMessage();
        await settle();
        assert.equal(h.rendered[0].content.textContent, expected);
        assert.equal(h.requests.length, 1);
        assert.equal(h.retries.length, 0);
        assert.equal(h.ctx.sendBtn.disabled, false);
    }
});

test('non-JSON failures retain HTTP context and server details render only as plain text', async () => {
    const h = setupSend(() => ({ ok: false, status: 401, json: async () => { throw Error('HTML response'); } }));
    h.ctx.sendMessage();
    await settle();
    assert.equal(h.rendered[0].content.textContent, 'error_login_required');
    assert.equal(h.retries.length, 0);
    const detail = '<img src=x onerror=alert(1)> [click](https://example.com)';
    const markup = setupSend(() => response({ status: 'error', message: detail }, 500));
    markup.ctx.sendMessage();
    await settle();
    assert.equal(markup.rendered[0].content.textContent, 'error_send ' + detail);
    assert.equal(markup.rendered[0].content.innerHTML, '');
});


test('a platform administrator without memberships is gated to assignment recovery, not platform management', async () => {
    const h = setup(async url => url === '/auth/check' ? response(database('admin'))
        : response({ status: 'success', user: { is_platform_admin: true }, tenants: [] }),
    { tenantResolution: true });
    h.storage.set('cow_tenant_id', 'removed');
    await settle();
    assert.equal(h.counter.init, 0);
    assert.equal(h.storage.has('cow_tenant_id'), false);
    // Restricted recovery gate: the app is NOT entered, so no normal page
    // (including the tenant management page) is shown as though a tenant were
    // selected. The login overlay + assignment notice remain visible with retry.
    assert.equal(h.node('app').classList.contains('hidden'), true);
    assert.equal(h.node('login-overlay').classList.contains('hidden'), false);
    assert.notEqual(h.ctx.currentView, 'tenant');
    const authCheckPanel = h.node('auth-check-panel');
    assert.equal(authCheckPanel.classList.contains('hidden'), false);
    assert.equal(h.node('auth-check-retry').classList.contains('hidden'), false);
    const send = setupSend(() => { throw Error('Must not dispatch without a tenant'); });
    send.ctx._identityMode = () => 'database';
    send.ctx.sendMessage();
    assert.equal(send.requests.length, 0);
    assert.equal(send.ctx.chatInput.value, 'hello');
    assert.equal(send.rendered[0].content.textContent, 'error_tenant_required');
});

test('chat initialization restores scoped selections and waits for the Agent catalog before session reads', async () => {
    const h = setup(async url => url === '/auth/check' ? response(database())
        : response({ status: 'success', version: '1.0' }));
    await settle();
    const catalog = deferred(), steps = [];
    h.ctx.activeAgentId = 'old-agent';
    h.ctx.sessionId = 'old-session';
    h.ctx.readScopedPreference = key => key === 'cow_active_agent' ? 'new-agent' : null;
    h.ctx.loadAgentCatalog = () => {
        steps.push(['catalog', h.ctx.activeAgentId]);
        return catalog.promise;
    };
    h.ctx.loadOrCreateSessionId = () => {
        steps.push(['session', h.ctx.activeAgentId]);
        return 'new-session';
    };
    h.ctx.refreshWorkspaceSelector = () => steps.push(['workspace', h.ctx.sessionId]);
    h.ctx.refreshSessionSettings = () => steps.push(['settings', h.ctx.sessionId]);
    h.ctx.restoreChatState = () => steps.push(['history', h.ctx.sessionId]);
    const ready = h.realInitApp();
    assert.deepEqual(steps, [['catalog', 'new-agent']]);
    catalog.resolve();
    await ready;
    assert.deepEqual(steps, [['catalog', 'new-agent'], ['session', 'new-agent'],
        ['workspace', 'new-session'], ['settings', 'new-session'], ['history', 'new-session']]);
});

test('navigation availability gate reads the authoritative /auth/context projection, not a client role array', () => {
    const h = setup(async url => url === '/auth/check' ? response(database())
        : response({ status: 'success', identity_mode: 'database' }));
    // Force database mode so _viewNavDenied actually evaluates.
    h.ctx._identityMode = () => 'database';
    // The VM does not load VIEW_META (defined in a non-selected section), so we
    // stub the getter with the mapping that _viewNavDenied relies on.
    h.run("_consolePageForView = v => v === 'roles' ? 'admin.roles' : (v === 'skills' ? 'admin.skills' : '')");
    // A member (not platform admin) with a known projection that denies 'roles'.
    h.run("_authContext = { status: 'success', authorization_mode: 'role', is_tenant_admin: false, console_pages: { 'admin.roles': { available: false, read_allowed: false, reason: 'no_resource_grant' } } }");
    const denied = h.run('_viewNavDenied("roles")');
    assert.equal(denied && denied.reason, 'denied');
    // 'skills' is not signed by this projection -> allowed (do not guess).
    assert.equal(h.run('_viewNavDenied("skills")'), null);
    // Platform 'all' mode overrides the same denial.
    h.run("_authContext = { status: 'success', authorization_mode: 'all', console_pages: {} }");
    assert.equal(h.run('_viewNavDenied("roles")'), null);
    // No projection loaded yet -> unknown -> allowed (must not block startup).
    h.run('_authContext = null');
    assert.equal(h.run('_viewNavDenied("roles")'), null);
});

test('sidebar label prefers the current tenant member display name over the account name', async () => {
    // Account-level display name is "ADMIN"; the selected tenant's member
    // display name is "平台管理员". On entry the cached /auth/me projection
    // should drive the sidebar to the member name for the selected tenant.
    const h = setup(async (url, options) => {
        if (url === '/auth/check') return response(database('admin'));
        if (url === '/auth/me') return response({
            status: 'success',
            user: { username: 'admin', display_name: 'ADMIN' },
            tenants: [{ id: 't1', code: 'default', name: '默认租户',
                membership: { display_name: '平台管理员', roles: [], department: null,
                    position_text: '' } }],
        });
        return response({ status: 'success' });
    }, { tenantResolution: true });
    h.storage.set('cow_tenant_id', 't1');
    await settle();
    // After init the account self is fetched and the label should be the
    // member display name for the selected tenant.
    assert.equal(h.storage.get('cow_tenant_id'), 't1');
    assert.equal(h.run('_currentMemberDisplayName()'), '平台管理员');
    // The rendered sidebar name uses the member display name once the self
    // projection has resolved.
    await h.ctx.refreshAccountIdentity();
    await settle();
    assert.equal(h.node('sidebar-account-name').textContent, '平台管理员');
});

test('sidebar falls back to the account name when the selected tenant has no member display name', async () => {
    const h = setup(async (url, options) => {
        if (url === '/auth/check') return response(database('admin'));
        if (url === '/auth/me') return response({
            status: 'success',
            user: { username: 'admin', display_name: 'ADMIN' },
            tenants: [{ id: 't1', membership: { display_name: '  ', roles: [] } }],
        });
        return response({ status: 'success' });
    }, { tenantResolution: true });
    h.storage.set('cow_tenant_id', 't1');
    await settle();
    assert.equal(h.run('_currentMemberDisplayName()'), '', 'whitespace member name must not override');
    await h.ctx.refreshAccountIdentity();
    await settle();
    assert.equal(h.node('sidebar-account-name').textContent, 'ADMIN');
});

test('sidebar ignores the member name when no tenant is selected or the account is not a member', async () => {
    const h = setup(async (url, options) => {
        if (url === '/auth/check') return response(database('admin'));
        if (url === '/auth/me') return response({
            status: 'success',
            user: { username: 'admin', display_name: 'ADMIN' },
            tenants: [],
        });
        return response({ status: 'success' });
    }, { tenantResolution: true });
    h.storage.set('cow_tenant_id', 't1');
    await settle();
    // No membership for t1 -> no member name; account name retained.
    assert.equal(h.run('_currentMemberDisplayName()'), '');
    await h.ctx.refreshAccountIdentity();
    await settle();
    assert.equal(h.node('sidebar-account-name').textContent, 'ADMIN');
});

// ---- bundled default account avatars -------------------------------------
// Five static SVGs stand in for an account that never uploaded a picture. The
// disc is chosen from the account id alone, so it is stable across refreshes,
// tenants and list order, and an uploaded picture always wins.

test('the default avatar mapping is bounded, stable and derived from the id', async () => {
    const h = setup();
    await settle();
    // Pinned values: a formula change must fail loudly, not silently reshuffle
    // everyone's default face.
    assert.equal(h.run("userDefaultAvatarIndex('usr-alice')"), 3);
    assert.equal(h.run("userDefaultAvatarIndex('usr-bob')"), 5);
    assert.equal(h.run("userDefaultAvatarIndex('usr-carol')"), 2);
    assert.equal(h.run("userDefaultAvatarURL('usr-bob')"), '/assets/avatars/default-5.svg');
    for (const id of ['', 'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']) {
        const n = h.run(`userDefaultAvatarIndex(${JSON.stringify(id)})`);
        assert.ok(n >= 1 && n <= 5, `index ${n} out of range for ${JSON.stringify(id)}`);
    }
});

test('sidebar and account-menu discs render the bundled default when nothing was uploaded', async () => {
    const h = setup(async () => response(database('alice',
        { user: { id: 'usr-alice', username: 'alice', display_name: 'Alice' } })));
    await settle();
    const html = h.node('sidebar-account-avatar').innerHTML;
    assert.match(html, /<img class="user-avatar"/);
    assert.match(html, /src="\/assets\/avatars\/default-3\.svg"/);
    // The 404-on-missing-upload path degrades to the same default.
    assert.match(html, /onerror=/);
    assert.equal(h.node('account-menu-avatar').innerHTML, html);
});

test('an uploaded picture takes priority and is read from the personal endpoint', async () => {
    const h = setup(async () => response(database('alice',
        { user: { id: 'usr-alice', username: 'alice', display_name: 'Alice', avatar: 'image' } })));
    await settle();
    const html = h.node('sidebar-account-avatar').innerHTML;
    assert.match(html, /src="\/auth\/profile\/avatar"/);
    assert.doesNotMatch(html, /\/api\/users\//);
    // Even then a broken upload falls back to the id-stable default.
    assert.match(html, /default-3\.svg/);
});

test('the default avatar is stable for one id across refreshes and name changes', async () => {
    let user = { id: 'usr-alice', username: 'alice', display_name: 'Alice' };
    const h = setup(async () => response(database('alice', { user })));
    await settle();
    const first = h.node('sidebar-account-avatar').innerHTML;
    assert.match(first, /default-3\.svg/);
    // A rename must not reshuffle the default face.
    user = { id: 'usr-alice', username: 'alice', display_name: 'Completely Different Name' };
    await h.ctx.refreshAccountIdentity();
    await settle();
    assert.equal(h.node('sidebar-account-avatar').innerHTML, first);
});

test('another account\'s upload is read through the by-id route, with a default fallback', async () => {
    const h = setup();
    await settle();
    const html = h.run("userAvatarHTML({ id: 'usr-carol', avatar: 'image' })");
    assert.match(html, /src="\/api\/users\/usr-carol\/avatar"/);
    assert.match(html, /default-2\.svg/);
    // An upload that 404s (flag set, file gone) falls back, never a broken img.
    assert.match(html, /onerror=/);
});

// ---- profile identity chips ----------------------------------------------
// Platform identity and tenant roles are attributes, not prose: they render as
// rounded chips so the profile dialog has a visible hierarchy. Role names come
// from the identity store, so they must be escaped like any other projection.

test('profile renders platform identity and tenant roles as escaped chips', async () => {
    const h = setup();
    await settle();
    h.storage.set('cow_tenant_id', 't1');
    h.run(`_accountSelf = { status: 'success', user: { id: 'usr-alice', username: 'alice',
        display_name: 'Alice', is_platform_admin: true }, tenants: [{ id: 't1', name: 'Tenant One',
        membership: { display_name: 'Alice',
            roles: [{ name: '租户管理员' }, { name: '<img src=x onerror=alert(1)>' }],
            department: null, position_text: '' } }] };`);
    h.ctx.renderAccountProfile();
    const platform = h.node('ap-platform');
    assert.match(platform.innerHTML, /<span class="account-profile-chip">/);
    assert.match(platform.innerHTML, /platform_admin_badge/);
    const role = h.node('ap-role');
    assert.match(role.innerHTML, /<span class="account-profile-chip">租户管理员<\/span>/);
    assert.equal((role.innerHTML.match(/class="account-profile-chip"/g) || []).length, 2);
    assert.ok(!role.innerHTML.includes('<img'), 'role names must not inject markup');
    assert.match(role.innerHTML, /&lt;img/);
});

test('profile falls back to plain copy when identity or roles are absent', async () => {
    const h = setup();
    await settle();
    h.storage.set('cow_tenant_id', 't1');
    h.run(`_accountSelf = { status: 'success', user: { id: 'usr-bob', username: 'bob',
        display_name: 'Bob', is_platform_admin: false }, tenants: [{ id: 't1', name: 'Tenant One',
        membership: { display_name: 'Bob', roles: [], department: null, position_text: '' } }] };`);
    h.ctx.renderAccountProfile();
    assert.equal(h.node('ap-platform').textContent, 'account_public_mode');
    assert.equal(h.node('ap-platform').innerHTML, '');
    assert.equal(h.node('ap-role').textContent, 'account_profile_empty');
    assert.equal(h.node('ap-role').innerHTML, '');
});

// ---- avatar change gesture -----------------------------------------------
// Changing the avatar is a double-click so a casual single click in the hero
// cannot fire a native file dialog. Keyboard activation stays available.

test('the avatar picker ignores the first click and opens on the second or via keyboard', async () => {
    const h = setup();
    await settle();
    const input = h.node('account-profile-avatar-input');
    let opens = 0;
    input.click = () => { opens++; };
    // First click of a pointer double-click: must not open, must not clear.
    input.value = 'already-picked.png';
    h.ctx.handleAccountProfileAvatarActivate({ detail: 1 });
    assert.equal(opens, 0, 'the first click of a double-click must not open');
    assert.equal(input.value, 'already-picked.png');
    // Second click: opens, and the value is cleared so re-picking works.
    h.ctx.handleAccountProfileAvatarActivate({ detail: 2 });
    assert.equal(opens, 1);
    assert.equal(input.value, '', 'the input is cleared so the same file can be re-picked');
    // Keyboard activation reports detail 0 and must keep working.
    h.ctx.handleAccountProfileAvatarActivate({ detail: 0 });
    assert.equal(opens, 2);
    // A synthetic event without a click count is treated as keyboard, not dead.
    h.ctx.handleAccountProfileAvatarActivate(undefined);
    assert.equal(opens, 3);
});

test('the avatar picker does not stack while an upload is in flight', async () => {
    const h = setup();
    await settle();
    const input = h.node('account-profile-avatar-input');
    let opens = 0;
    input.click = () => { opens++; };
    h.run('_accountProfileAvatarUploading = true;');
    h.ctx.handleAccountProfileAvatarActivate({ detail: 2 });
    assert.equal(opens, 0);
    h.run('_accountProfileAvatarUploading = false;');
    h.ctx.handleAccountProfileAvatarActivate({ detail: 2 });
    assert.equal(opens, 1);
});

test('the avatar file input lives outside the repainted disc in chat.html', () => {
    // renderAccountProfileAvatar rewrites the disc's innerHTML on every load; an
    // input nested inside it would be deleted and the picker would silently die.
    const html = fs.readFileSync(path.join(__dirname, '../channel/web/chat.html'), 'utf8');
    const buttonId = html.indexOf('id="account-profile-avatar"');
    const input = html.indexOf('id="account-profile-avatar-input"');
    assert.ok(buttonId >= 0 && input >= 0, 'both the disc and the input must exist');
    assert.ok(input > buttonId, 'the input documents after the disc');
    // Slice from the disc's own `<button` tag, so the pairing below is a real
    // nesting check rather than an artifact of where the id attribute sits.
    const elementStart = html.lastIndexOf('<button', buttonId);
    assert.ok(elementStart >= 0 && elementStart < buttonId, 'the disc is a <button>');
    const between = html.slice(elementStart, input);
    const opened = (between.match(/<button\b/g) || []).length;
    const closed = (between.match(/<\/button>/g) || []).length;
    assert.equal(opened, 1, 'exactly the disc button opens before the input');
    assert.equal(closed, 1, 'the disc button is closed, so the input is not nested inside it');
    // The disc carries the double-click semantics through the handler name.
    assert.match(between, /handleAccountProfileAvatarActivate\(event\)/);
});

test('the account panel keeps its region order and adds no personal page tab', () => {
    // The account panel is an account surface: the regions are identity →
    // 账号设置 → help/logout → brand version. The 「我的资源」 region between
    // identity and settings was removed (change unify-console-by-data-scope,
    // task 3.3), and nothing took its place: business resources are reached
    // through the console, so no new personal group may appear here.
    const html = fs.readFileSync(path.join(__dirname, '../channel/web/chat.html'), 'utf8');
    const order = ['account-menu-identity', 'account-menu-settings',
        'account-menu-about', 'account-menu-logout', 'sidebar-version']
        .map(id => html.indexOf('id="' + id + '"'));
    assert.ok(order.every(index => index >= 0), 'every region must exist');
    assert.deepEqual([...order].sort((a, b) => a - b), order,
        'identity → account settings → help/logout → version');
    const menuStart = html.indexOf('id="sidebar-account-menu"');
    const menuEnd = html.indexOf('id="account-menu-backdrop"', menuStart);
    assert.ok(menuStart >= 0 && menuEnd > menuStart, 'the panel markup is delimited');
    const menu = html.slice(menuStart, menuEnd);
    assert.doesNotMatch(menu, /role="tablist"/, 'the panel adds no tab strip');
    assert.doesNotMatch(html, /id="personal-console-tabs"/, 'no personal top tabs');
    assert.doesNotMatch(html, /id="personal-shortcuts"/, 'no in-page personal shortcut bar');
    // One group title, and it is a label rather than a disclosure: the entries
    // under it are directly actionable.
    assert.equal((html.match(/data-i18n="account_menu_settings"/g) || []).length, 1);
    assert.doesNotMatch(html, /data-i18n="account_menu_resources/);
    assert.doesNotMatch(menu, /<details/, 'the settings group is not a disclosure');
});

test('the console top bar does not repeat the logout, appearance or language entries', () => {
    // console-entry-consistency: the account menu owns profile, account
    // security, preferences, help/about and logout, so the new layout must not
    // keep duplicate logout, standalone language or appearance entries in the
    // top bar. This pins the removal so the duplicate entries cannot come back
    // without deleting the test.
    const html = fs.readFileSync(path.join(__dirname, '../channel/web/chat.html'), 'utf8');
    const start = html.indexOf('<header class="workbench-header');
    const end = html.indexOf('</header>', start);
    assert.ok(start >= 0 && end > start, 'the workbench header must exist');
    const header = html.slice(start, end);
    assert.doesNotMatch(header, /id="logout-btn-header"/, 'no duplicate logout entry in the top bar');
    assert.doesNotMatch(header, /id="theme-toggle"/, 'no duplicate appearance entry in the top bar');
    assert.doesNotMatch(header, /id="lang-selector"/, 'no standalone language entry in the top bar');
    assert.doesNotMatch(header, /id="lang-menu"/, 'no standalone language menu in the top bar');
    assert.match(header, /id="tenant-selector"/, 'the tenant context stays in the top bar');
    // The view-level workspace action owns the right edge of the top bar, so it
    // must follow the tenant context instead of sitting in front of it.
    const tenantIndex = header.indexOf('id="tenant-selector"');
    const workspaceIndex = header.indexOf('id="workspace-toggle-btn"');
    assert.ok(tenantIndex > 0 && workspaceIndex > tenantIndex,
        'the workspace toggle must be the right-most entry in the top bar');
    // The removed entries keep exactly one home: the account menu.
    assert.match(html, /id="account-menu-logout"/);
    assert.match(html, /id="account-menu-prefs"/);
});
