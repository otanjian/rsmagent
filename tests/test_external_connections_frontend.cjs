// 系统接入 console page (change add-external-system-access, task group 10).
//
// The page's two load-bearing promises are honesty constraints, so they are
// asserted against the code the browser actually runs rather than restated here:
//   * the test/execute classes are closed by the deployment, and the page renders
//     the server's own reason on a present-but-disabled control — it never
//     simulates a result and never offers a button that can only fail;
//   * credentials live only in the open form — never in a URL, never in
//     localStorage/sessionStorage, and a version conflict preserves what the user
//     typed instead of overwriting it.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const read = p => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const chatHtml = read('channel/web/chat.html');
const consoleJs = read('channel/web/static/js/console.js');
const pageJs = read('channel/web/static/js/external-connections.js');
const i18nJs = read('channel/web/static/js/i18n/external-connections.js');
const css = read('channel/web/static/css/console.css');

// -- the namespace, loaded the way console.js merges it -------------------
function loadNamespaces() {
    const ctx = { window: {} };
    vm.createContext(ctx);
    vm.runInContext(i18nJs, ctx, { filename: 'external-connections.js' });
    return ctx.window.__cowI18N__['external-connections'];
}
const ns = loadNamespaces();
const zh = ns.zh;

// -- minimal DOM -----------------------------------------------------------
// -- the tiny bit of markup reading the stub needs -------------------------
// A value read from a rendered control has to come from the markup the page
// wrote, otherwise "what the user typed" would be fabricated by the test.
function attrOf(tag, name) {
    const quoted = new RegExp(name + '\\s*=\\s*"([^"]*)"').exec(tag);
    if (quoted) return quoted[1];
    return new RegExp('(?:^|\\s)' + name + '(?=[\\s/>])').test(tag) ? '' : null;
}

function tagMarkup(html, sel) {
    if (!html) return '';
    const tags = html.match(/<[a-zA-Z][^>]*>/g) || [];
    if (sel.charAt(0) === '#') {
        const id = sel.slice(1);
        return tags.find(t => attrOf(t, 'id') === id) || '';
    }
    const attr = /^\[([A-Za-z0-9_-]+)(?:="([^"]*)")?\]$/.exec(sel);
    if (attr) {
        return tags.find(t => {
            const value = attrOf(t, attr[1]);
            if (value === null) return false;
            return attr[2] === undefined || value === attr[2];
        }) || '';
    }
    if (/^[a-z]+$/.test(sel)) return tags.find(t => t.toLowerCase().indexOf('<' + sel) === 0) || '';
    return '';
}

function valueFromMarkup(html, tag) {
    if (!tag) return '';
    if (tag.toLowerCase().indexOf('<textarea') === 0) {
        const start = html.indexOf(tag) + tag.length;
        const end = html.indexOf('</textarea>', start);
        return end > start ? html.slice(start, end) : '';
    }
    if (tag.toLowerCase().indexOf('<select') === 0) {
        const start = html.indexOf(tag) + tag.length;
        const end = html.indexOf('</select>', start);
        const body = end > start ? html.slice(start, end) : '';
        const options = body.match(/<option[^>]*>/g) || [];
        const chosen = options.find(o => attrOf(o, 'selected') !== null) || options[0];
        return chosen ? (attrOf(chosen, 'value') || '') : '';
    }
    const value = attrOf(tag, 'value');
    return value === null ? '' : value;
}

// Elements answer selector lookups with a memoized stub, so the page's real
// code path runs (it attaches listeners to whatever `querySelector` returns)
// while the test can inject values and fire those listeners. `querySelectorAll`
// stays empty on purpose: DOM-wide collection re-renders are not what these
// assertions are about, and an empty list keeps the draft the test set intact.
function element(tag) {
    const el = {
        tagName: String(tag || 'div').toUpperCase(),
        id: '', className: '', hidden: false, value: '', type: 'text',
        textContent: '', style: {}, children: [],
        parentNode: null, _key: null, _q: Object.create(null),
        _attrs: Object.create(null),
        classList: {
            add() {}, remove() {}, toggle() {}, contains() { return false; },
        },
        setAttribute(name, value) { el._attrs[name] = String(value); },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(el._attrs, name) ? el._attrs[name] : null;
        },
        removeAttribute(name) { delete el._attrs[name]; },
        addEventListener(type, fn) { harness.handlers(el._key + ' ' + type, []).push(fn); },
        removeEventListener() {},
        appendChild(child) { el.children.push(child); return child; },
        insertBefore(child) { el.children.push(child); return child; },
        removeChild() {},
        querySelector(sel) {
            const key = el._key + ' > ' + sel;
            if (!el._q[sel]) {
                const source = el._ctx ? el._ctx.html : el.innerHTML;
                const tag = tagMarkup(source, sel);
                const child = element('div');
                child._key = key;
                child.parentNode = el;
                child._ctx = { html: source, tag };
                child.value = valueFromMarkup(source, tag);
                el._q[sel] = child;
            }
            return el._q[sel];
        },
        querySelectorAll() { return []; },
        closest() { return null; },
        focus() { harness.focus(el); }, setSelectionRange() {},
    };
    // Replacing the markup of a container is what the page does between modals;
    // the stub drops its old lookup elements and their listeners with it, so a
    // confirmation cannot fire a handler registered by an earlier modal.
    let html = '';
    Object.defineProperty(el, 'innerHTML', {
        get() { return html; },
        set(value) {
            html = String(value);
            el._q = Object.create(null);
            harness.clearDescendants(el._key);
            harness.rendered(el, html);
        },
    });
    return el;
}

// Listener registry for the stub elements, keyed by "<element key> <event>".
const harness = {
    store: new Map(),
    handlers(key, fallback) {
        if (!this.store.has(key)) this.store.set(key, fallback);
        return this.store.get(key);
    },
    fire(key, event) {
        (this.store.get(key) || []).forEach(fn => fn(event || { target: {} }));
    },
    // Drop listeners registered below a container that just re-rendered.
    clearDescendants(key) {
        for (const storeKey of Array.from(this.store.keys())) {
            if (storeKey.indexOf(key + ' > ') === 0) this.store.delete(storeKey);
        }
    },
    reset() { this.store.clear(); this.focused = []; this.doc = null; this.renders = []; },
    // Every markup the page has put into a container, in order. A container that
    // is replaced and restored inside a single turn of the event loop — which is
    // what the stubbed fetch does — still went through both states, so this is
    // the only honest record of the frames in between.
    renders: [],
    rendered(el, html) { this.renders.push({ key: el._key, html }); },
    // Where focus went, in order. A repaint that replaces the panel takes the
    // previously focused node with it, so the page's only way to keep a keyboard
    // user in place is to call `focus()` again — which has to be observable.
    focused: [],
    doc: null,
    focus(el) {
        this.focused.push(el);
        if (this.doc) this.doc.activeElement = el;
    },
};

function boot(options) {
    const opts = options || {};
    const nodes = new Map();
    const docQueries = Object.create(null);
    harness.reset();
    let keySeq = 0;
    const document = {
        activeElement: null,
        documentElement: element('html'),
        addEventListener() {},
        createElement(tag) { return element(tag); },
        getElementById(id) {
            if (!nodes.has(id)) {
                const node = element('div');
                node.id = id;
                node._key = '#id:' + id + ':' + (keySeq += 1);
                nodes.set(id, node);
            }
            return nodes.get(id);
        },
        // A document-level lookup only answers when the selector's own marker is
        // in the modal currently rendered, so a lookup for a block the page did
        // not render behaves like the real DOM and returns null.
        querySelector(sel) {
            const html = (nodes.get('ec-modal-panel') || {}).innerHTML || '';
            const marker = sel.replace(/[\[\]"'.#]/g, '').split('=')[0];
            if (marker && html.indexOf(marker) === -1) return null;
            const key = 'doc > ' + sel;
            if (!docQueries[key]) {
                const node = element('div');
                node._key = key;
                docQueries[key] = node;
            }
            return docQueries[key];
        },
        querySelectorAll() { return []; },
    };
    document.body = element('body');
    document.activeElement = document.body;
    // The element stub's `focus()` writes here, so a test can ask where focus
    // ended up after the page repainted a panel.
    harness.doc = document;

    const fetchCalls = [];
    const toasts = [];
    const localStorageWrites = [];
    const sessionWrites = [];
    // Debounce/search timers are captured rather than run: "nothing was asked
    // yet" is a promise of the debounce, and a test has to be able to observe
    // the moment before it fires.
    const timers = [];
    let timerSeq = 0;
    let responder = opts.responder || (() => ({ ok: true, status: 200, json: { status: 'success' } }));

    const ctx = {
        console: { warn() {}, error() {}, log() {} },
        URL,
        Promise,
        setInterval: () => 0,
        clearInterval: () => {},
        setTimeout(fn, ms) { const id = (timerSeq += 1); timers.push({ id, fn, ms }); return id; },
        clearTimeout(id) {
            const index = timers.findIndex(timer => timer.id === id);
            if (index >= 0) timers.splice(index, 1);
        },
        confirm: () => true,
        localStorage: {
            getItem: () => null,
            setItem(k, v) { localStorageWrites.push([k, v]); },
            removeItem(k) { localStorageWrites.push([k, null]); },
        },
        sessionStorage: {
            // The console shell carries the current tenant here; only a test that
            // asks for it gets one, so the other tests keep reading one scope.
            getItem: k => (k === 'cow_tenant_id' ? (opts.tenantId || null) : null),
            setItem(k, v) { sessionWrites.push([k, v]); },
            removeItem(k) { sessionWrites.push([k, null]); },
        },
        fetch(url, init) {
            fetchCalls.push({ url, init });
            const out = responder(url, init);
            return Promise.resolve({
                ok: out.status ? out.status >= 200 && out.status < 300 : true,
                status: out.status || 200,
                json: () => Promise.resolve(out.json === undefined ? { status: 'success' } : out.json),
            });
        },
        t(key) { return Object.prototype.hasOwnProperty.call(zh, key) ? zh[key] : key; },
        crypto: { randomUUID: () => 'uuid-' + fetchCalls.length },
        navigateTo() {},
        // Recorded rather than discarded: what the console tells the user after a
        // test is part of the promise, and a stub that swallows it would let the
        // page report a verdict the server never gave.
        _wsToast(message, tone) { toasts.push([message, tone]); },
        registerConsoleView(spec) { ctx.__specs.push(spec); },
        __specs: [],
        document,
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(pageJs, ctx, { filename: 'external-connections.js' });
    return {
        ctx, document, fetchCalls, toasts, localStorageWrites, sessionWrites,
        page: ctx.ExternalConnectionsPage,
        specs: ctx.__specs,
        nodes, harness,
        setResponder(fn) { responder = fn; },
        // DOM handles for the operation flows: the modal panel the page renders
        // into and its elements, plus the recorded listeners.
        panel() { return nodes.get('ec-modal-panel') || document.getElementById('ec-modal-panel'); },
        panelEl(sel) { return this.panel().querySelector(sel); },
        drawerBody() { return document.getElementById('ec-drawer-body'); },
        // The catalogue list, as the browser would show it.
        root() { return document.getElementById('ec-root'); },
        // The test control of one card, looked up the way the page looks it up.
        testControl(id) {
            return this.root().querySelector(
                '[data-ec-action="test"][data-id="' + id + '"]');
        },
        fire(node, type, extra) {
            const event = Object.assign({ target: node }, extra || {});
            harness.fire(node._key + ' ' + type, event);
        },
        // Run every captured timer, in order, once: what the debounce was
        // waiting for happened, exactly once.
        runTimers() {
            const due = timers.splice(0, timers.length);
            due.forEach(timer => timer.fn());
            return due.length;
        },
        pendingTimers() { return timers.length; },
    };
}

const MCP_TYPE = {
    kind: 'mcp',
    label: 'MCP',
    label_key: 'ext_conn_type_mcp',
    config_keys: ['transport', 'url'],
    secret_slots: ['header', 'env', 'oauth'],
    scopes: [
        { scope: 'platform', available: true, reason: '' },
        { scope: 'tenant', available: false, reason: 'management_required' },
    ],
    // The deployment has not accepted an MCP test environment: the class is
    // closed and the reason is the server's, not a placeholder.
    capabilities: {
        open: ['configure'],
        test_available: false,
        execute_available: false,
        unavailable_reason: 'awaiting_mcp_test_environment',
        missing_secret_slots: ['header'],
    },
};

const MCP_CARD = {
    id: 'card-1', effective_id: 'card-1', kind: 'mcp', scope: 'platform', name: 'Shared tools',
    enabled: true, source: 'platform', version: 1, test_status: 'untested', tested_at: null,
    actions: ['read', 'manage'], base_connection_id: null,
};

function typesResponse(types, scopes) {
    return {
        status: 'success',
        types: types || [MCP_TYPE],
        form_version: 1,
        scopes: scopes,
    };
}

const okCatalog = items => ({
    status: 'success', items: items || [], total: (items || []).length, scope: 'platform',
});

// The last POST the page sent (a GET after a successful save is not a write).
const lastWrite = app => app.fetchCalls.filter(
    call => call.init && call.init.method === 'POST').pop();

// =====================================================================
// 1. Registration: menu entry, group order, view meta, lazy module
// =====================================================================
// =====================================================================
// 1b. The displayed name, in one place and consistent
// =====================================================================
// The page's name is written in four places that must agree: the sidebar menu,
// the page title, the static fallback inside chat.html (which is what shows
// before the module loads or when a translation is missing), and the menu
// resource name the role editor lists. Nothing asserted any of them, so the
// four could drift apart — which the navigation requirements forbid.
const PAGE_NAME = { zh: '系统接入', 'zh-Hant': '系統接入', en: 'System Access' };
const OLD_PAGE_NAME = { zh: '外部系统接入', 'zh-Hant': '外部系統接入', en: 'External System Access' };

test('the displayed name is the same in the menu, the title and every language', () => {
    for (const lang of Object.keys(PAGE_NAME)) {
        const strings = ns[lang];
        assert.ok(strings, 'the namespace carries ' + lang);
        assert.equal(strings.menu_external_connections, PAGE_NAME[lang],
            lang + ': the sidebar entry uses the page name');
        assert.equal(strings.ec_title, PAGE_NAME[lang],
            lang + ': the page heading uses the same name as the sidebar entry');

        // The fallback markup is what a user sees before the module loads. Like
        // every other entry in chat.html it is the default-language copy, so it
        // is held to the simplified value — a stale one would show the old name,
        // and any other language's value would be a different page's name.
        const marked = chatHtml.indexOf('data-i18n="menu_external_connections"');
        const element = chatHtml.slice(chatHtml.lastIndexOf('<', marked));
        const fallback = /^<[^>]*>([^<]*)</.exec(element);
        assert.ok(fallback, 'the sidebar entry has its fallback text');
        assert.equal(fallback[1], ns.zh.menu_external_connections,
            'the static fallback equals the default-language name, so nothing flashes');
    }
});

test('no user-visible string still carries the old name', () => {
    for (const lang of Object.keys(OLD_PAGE_NAME)) {
        const strings = ns[lang];
        for (const key of ['menu_external_connections', 'ec_title']) {
            assert.notEqual(strings[key], OLD_PAGE_NAME[lang],
                lang + '/' + key + ' must not keep the old name');
        }
    }
    const entryStart = chatHtml.indexOf('data-i18n="menu_external_connections"');
    const entryLine = chatHtml.slice(entryStart, chatHtml.indexOf('</a>', entryStart));
    for (const lang of Object.keys(OLD_PAGE_NAME)) {
        assert.equal(entryLine.indexOf(OLD_PAGE_NAME[lang]), -1,
            'the sidebar markup must not keep the old ' + lang + ' name');
    }
});

test('the menu entry sits in 模型与接入, immediately after 消息渠道', () => {
    const from = chatHtml.indexOf('data-group="model-access"');
    const to = chatHtml.indexOf('<!-- 组织与权限', from);
    assert.ok(from > 0 && to > from, 'the 模型与接入 group must exist');
    const group = chatHtml.slice(from, to);
    const config = group.indexOf('data-view="config"');
    const channels = group.indexOf('data-view="channels"');
    const external = group.indexOf('data-view="external_connections"');
    assert.ok(config > 0 && channels > config && external > channels,
        'order must be 模型服务 → 消息渠道 → 系统接入');
    assert.match(group, /data-i18n="menu_external_connections"/);
    // Nothing between 消息渠道 and the new entry: "immediately after".
    const between = group.slice(channels + 'data-view="channels"'.length, external);
    assert.equal((between.match(/data-view=/g) || []).length, 0);
});

test('console.js signs the view with a stable group and lazy module', () => {
    assert.match(consoleJs,
        /'external_connections':\s*\{\s*group:\s*'nav_group_model_access',\s*page:\s*'menu_external_connections',\s*console:\s*'admin\.external_connections'\s*\}/);
    assert.match(consoleJs, /external_connections:\s*'assets\/js\/external-connections\.js'/);
    // The page body is a container only; the page module is not a <script> in
    // chat.html (only its i18n namespace file is), so nothing is fetched until
    // the view is entered.
    assert.match(chatHtml, /id="view-external_connections"/);
    assert.doesNotMatch(chatHtml, /<script[^>]*src="assets\/js\/external-connections\.js"/);
    assert.match(chatHtml, /<script defer src="assets\/js\/i18n\/external-connections\.js"><\/script>/);
    // classic/split share one sidebar; the entry is a plain sidebar-item.
    assert.match(chatHtml, /class="sidebar-item[^"]*"\s*\n?\s*data-view="external_connections"/);
});

test('the module registers the page with its route, title and leave guards', () => {
    const app = boot();
    assert.equal(app.specs.length, 1);
    const spec = app.specs[0];
    assert.equal(spec.id, 'external_connections');
    assert.equal(spec.title, 'ec_title');
    assert.equal(spec.page, 'menu_external_connections');
    assert.equal(typeof spec.load, 'function');
    assert.equal(typeof spec.isDirty, 'function');
    assert.equal(typeof spec.confirmLeave, 'function');
    assert.equal(typeof spec.onLeave, 'function');
    // The page owns its own addresses; the alias normalises to the view id.
    assert.match(consoleJs, /viewId === 'external-connections' \? 'external_connections' : viewId/);
});

test('every i18n key the page renders exists in all three languages', () => {
    const keys = new Set();
    const quoted = /'(ec_[a-z0-9_]+|ext_conn_type_[a-z0-9_]+)'/g;
    let match;
    while ((match = quoted.exec(pageJs)) !== null) keys.add(match[1]);
    assert.ok(keys.size > 60, 'the page should name its keys, not inline copy');
    ['zh', 'zh-Hant', 'en'].forEach(lang => {
        keys.forEach(key => {
            assert.ok(Object.prototype.hasOwnProperty.call(ns[lang], key),
                `${lang} is missing ${key}`);
            assert.ok(String(ns[lang][key]).length > 0, `${lang}.${key} must not be empty`);
        });
    });
});

// =====================================================================
// 2. The closed test/execute state is the server's, never simulated
// =====================================================================
test('a closed test class renders the server reason on a disabled control', async () => {
    const app = boot({
        responder(url) {
            if (url.indexOf('/types') >= 0) return { json: typesResponse([MCP_TYPE]) };
            if (url.indexOf('/catalog') >= 0) return { json: okCatalog([MCP_CARD]) };
            return { json: { status: 'success' } };
        },
    });
    await app.page.load();
    const html = app.page.pageHtml();
    // The real reason sentence, taken from the deployment's own answer. It
    // states the *cause* only — which class is closed is carried by the label
    // next to it, so the same code can justify a closed test class here and a
    // closed execution class on a card that has testing open.
    assert.match(html, /尚未取得 MCP 测试环境的验收证据。/);
    assert.match(html, /缺少凭据：认证请求头/);
    // Present but disabled, with an accessible description.
    assert.match(html, /<button type="button" class="ec-btn ec-btn-ghost ec-btn-small" disabled/);
    assert.match(html, /aria-describedby="ec-test-reason-card-1"/);
    // Never a simulated success: no "连接正常" was invented anywhere.
    assert.doesNotMatch(html, /连接正常/);
    assert.doesNotMatch(html, /演示|模拟/);
});

test('a closed test class never fires a request, and stays closed after the read', async () => {
    const app = boot({
        responder(url) {
            if (url.indexOf('/types') >= 0) return { json: typesResponse([MCP_TYPE]) };
            if (url.indexOf('/catalog') >= 0) return { json: okCatalog([MCP_CARD]) };
            return { json: { status: 'success' } };
        },
    });
    await app.page.load();
    const card = app.page.state.cards[0];
    const before = app.fetchCalls.length;
    app.page.runTest(card);
    assert.equal(app.fetchCalls.length, before, 'a disabled control must not call the test route');
    assert.ok(app.page.capabilityFor('mcp').test_available === false);
});

test('a load failure is not rendered as "no connections"', async () => {
    const app = boot({
        responder(url) {
            if (url.indexOf('/types') >= 0) return { json: typesResponse([MCP_TYPE]) };
            return { status: 500, json: { status: 'error', code: 'internal', message: 'boom' } };
        },
    });
    await app.page.load();
    const html = app.page.pageHtml();
    assert.match(html, /连接目录读取失败/);
    assert.match(html, /这不代表没有连接/);
    assert.doesNotMatch(html, /还没有连接/);
    assert.doesNotMatch(html, /共 0 个连接/);
});

// =====================================================================
// 3. Secrets: only in the form, only in the request body
// =====================================================================
test('a typed credential goes into the request body and nowhere else', async () => {
    const app = boot({
        responder(url) {
            if (url.indexOf('/types') >= 0) return { json: typesResponse([MCP_TYPE]) };
            if (url.indexOf('/catalog') >= 0) return { json: okCatalog([]) };
            return { json: { status: 'success' } };
        },
    });
    await app.page.load();
    app.page.openDrawer({ mode: 'create', kind: 'mcp', scope: 'tenant', original: {} });
    app.page.setDraftValue('name', 'Tenant MCP');
    app.page.setDraftValue('transport', 'streamable_http');
    app.page.setDraftValue('url', 'https://mcp.example.com/mcp');
    app.page.setDraftValue('auth', 'header');
    app.page.setDraftValue('header_name', 'X-API-Key');
    app.page.setSecretInput('header', 'TOPSECRET-123');
    await app.page.save();

    const create = app.fetchCalls.filter(c => c.url.indexOf('/tenant') >= 0
        && c.init && c.init.method === 'POST')[0];
    assert.ok(create, 'the create request must be sent');
    assert.equal(create.url.indexOf('TOPSECRET-123'), -1, 'a secret never enters a URL');
    const body = JSON.parse(create.init.body);
    assert.deepEqual(body.secrets, { header: 'TOPSECRET-123' });
    assert.equal(body.config.header, undefined, 'a secret never enters the config');
    assert.equal(JSON.stringify(body).split('TOPSECRET-123').length - 1, 1,
        'the credential appears exactly once, in `secrets`');
    assert.equal(create.init.headers['Idempotency-Key'].length > 0, true,
        'a create carries an idempotency key');
    // Browser storage is never touched, in any form.
    assert.deepEqual(app.localStorageWrites, []);
    assert.deepEqual(app.sessionWrites, []);
    // The module persists nothing anywhere (no setItem at all), so a credential
    // has no durable home outside the live input.
    assert.equal(/\.setItem\s*\(/.test(pageJs), false, 'the page never writes storage');
    // A confirmed leave drops the live secret with the form.
    app.page.closeDrawer();
    assert.equal(app.page.drawerState(), null);
    assert.doesNotMatch(JSON.stringify(app.page.state), /TOPSECRET-123/);
});

test('blank means keep and an explicit clear sends null', async () => {
    const original = {
        kind: 'erp', scope: 'tenant', name: 'ERP', enabled: true, version: 3,
        actions: ['read', 'manage'], base_connection_id: null,
        config: { provider: 'rfc', ashost: 'sap.internal', sysnr: '00', client: '100', user: 'u', lang: 'EN' },
        secrets: { password: { configured: true } },
    };
    const app = boot({
        responder(url) {
            if (url.indexOf('/types') >= 0) return { json: typesResponse([]) };
            if (url.indexOf('/catalog') >= 0) return { json: okCatalog([]) };
            return { json: { status: 'success' } };
        },
    });
    await app.page.load();

    app.page.openDrawer({ mode: 'edit', kind: 'erp', scope: 'tenant', id: 'c1', version: 3, original });
    app.page.setDraftValue('name', 'ERP renamed');
    await app.page.save();
    const keep = JSON.parse(lastWrite(app).init.body);
    assert.equal(keep.expected_version, 3);
    assert.equal(keep.secrets, undefined, 'an untouched input keeps the stored credential');

    app.page.openDrawer({ mode: 'edit', kind: 'erp', scope: 'tenant', id: 'c1', version: 4, original });
    app.page.setClear('password', true);
    await app.page.save();
    const cleared = JSON.parse(lastWrite(app).init.body);
    assert.deepEqual(cleared.secrets, { password: null });
});

test('the MCP form carries no per-tool declaration, and the payload names no remote tool', async () => {
    const app = boot({
        responder(url) {
            if (url.indexOf('/types') >= 0) return { json: typesResponse([MCP_TYPE]) };
            if (url.indexOf('/catalog') >= 0) return { json: okCatalog([]) };
            return { json: { status: 'success' } };
        },
    });
    await app.page.load();
    app.page.openDrawer({ mode: 'create', kind: 'mcp', scope: 'tenant', original: {} });

    // 哪些远端工具可调用由服务器的发布决定，操作者不再逐条申明。
    const html = app.drawerBody().innerHTML;
    assert.ok(html.indexOf('data-ec-field="transport"') >= 0, 'the transport select is there');
    assert.equal(html.indexOf('read_only_tools'), -1,
        'neither a declaration field nor an error slot for one is rendered');

    app.page.setDraftValue('name', 'Knowledge MCP');
    app.page.setDraftValue('transport', 'streamable_http');
    app.page.setDraftValue('url', 'https://mcp.example.com/mcp');
    await app.page.save();

    const sent = JSON.parse(lastWrite(app).init.body);
    assert.deepEqual(Object.keys(sent.config).sort(), ['auth', 'transport', 'url'],
        'only connection-level keys travel; nothing enumerates remote tools');
    assert.equal(sent.secrets, undefined);
});

test('a stale declaration in a reloaded draft is neither validated nor resubmitted', async () => {
    const app = boot();
    // A connection saved by an older build still carries the retired key; the
    // form must neither resurrect the field nor fail the save on it.
    const draft = { name: 'x', transport: 'streamable_http', url: 'https://a.example.com/mcp',
        read_only_tools: 'knowledge_search\nknowledge search' };
    const errors = app.page.validateDraft({ kind: 'mcp', draft: draft, secrets: {}, secretInputs: {}, clears: {} });
    assert.deepEqual(Object.keys(errors), [],
        'no rule is attached to a field the form no longer owns');

    const app2 = boot({
        responder(url) {
            if (url.indexOf('/types') >= 0) return { json: typesResponse([MCP_TYPE]) };
            if (url.indexOf('/catalog') >= 0) return { json: okCatalog([]) };
            return { json: { status: 'success' } };
        },
    });
    await app2.page.load();
    const legacy = {
        kind: 'mcp', scope: 'tenant', name: 'Legacy MCP', enabled: true, version: 2,
        actions: ['read', 'manage'], base_connection_id: null,
        config: { transport: 'streamable_http', url: 'https://mcp.example.com/mcp', auth: 'none' },
        secrets: {},
    };
    app2.page.openDrawer({ mode: 'edit', kind: 'mcp', scope: 'tenant', id: 'c1', version: 2, original: legacy });
    app2.page.setDraftValue('read_only_tools', 'echo\necho');
    await app2.page.save();
    const resent = JSON.parse(lastWrite(app2).init.body);
    assert.equal(resent.config.read_only_tools, undefined,
        'the retired key is dropped on the way out, duplicated name and all');
});

test('a stdio MCP connection submits only its connection-level keys', async () => {
    const app = boot({
        responder(url) {
            if (url.indexOf('/types') >= 0) return { json: typesResponse([MCP_TYPE]) };
            if (url.indexOf('/catalog') >= 0) return { json: okCatalog([]) };
            return { json: { status: 'success' } };
        },
    });
    await app.page.load();
    app.page.openDrawer({ mode: 'create', kind: 'mcp', scope: 'tenant', original: {} });
    app.page.setDraftValue('name', 'Local tools');
    app.page.setDraftValue('transport', 'stdio');
    app.page.setDraftValue('command', 'npx');
    app.page.setDraftValue('args', '-y\nmcp-server');
    await app.page.save();

    const body = JSON.parse(lastWrite(app).init.body);
    assert.deepEqual(body.config, { transport: 'stdio', command: 'npx',
        args: ['-y', 'mcp-server'], env_keys: [] },
    'the stdio form declares how to reach the server, not which of its tools are callable');
});

test('a mask is refused as a credential instead of being submitted', async () => {
    const app = boot();
    const drawer = {
        kind: 'mcp', draft: { name: 'x', transport: 'streamable_http', url: 'https://a.example.com/mcp', auth: 'header', header_name: 'H' },
        secrets: { header: true }, secretInputs: { header: '••••••••' }, clears: {},
    };
    const errors = app.page.validateDraft(drawer);
    assert.equal(errors['secret:header'], zh.ec_validate_secret_masked);
    // And it never reaches the payload builder.
    assert.equal(Object.keys(app.page.buildSecrets(drawer)).length, 0);
});

// =====================================================================
// 4. A 409 preserves the input; nothing is auto-overwritten
// =====================================================================
test('a version conflict keeps the typed input and offers a re-read', async () => {
    const original = {
        kind: 'erp', scope: 'tenant', name: 'ERP', enabled: true, version: 3,
        actions: ['read', 'manage'], base_connection_id: null,
        config: { provider: 'rfc', ashost: 'sap.internal', sysnr: '00', client: '100', user: 'u', lang: 'EN' },
        secrets: { password: { configured: true } },
    };
    const app = boot({
        responder(url) {
            if (url.indexOf('/types') >= 0) return { json: typesResponse([]) };
            if (url.indexOf('/catalog') >= 0) return { json: okCatalog([]) };
            return { status: 409, json: { status: 'error', code: 'version_conflict', message: 'connection was modified by another writer' } };
        },
    });
    await app.page.load();
    app.page.openDrawer({ mode: 'edit', kind: 'erp', scope: 'tenant', id: 'c1', version: 3, original });
    app.page.setDraftValue('name', 'Renamed ERP');
    const before = app.fetchCalls.length;
    const saved = await app.page.save();
    assert.equal(saved, false);
    assert.equal(app.fetchCalls.length, before + 1, 'a conflict is not retried automatically');
    assert.equal(app.page.drawerState().draft.name, 'Renamed ERP', 'the input survives');
    assert.equal(app.page.drawerState().conflict, true);
    const body = app.document.getElementById('ec-drawer-body').innerHTML;
    assert.match(body, /为避免覆盖新版本/);
    assert.match(body, /data-ec-action="drawer-reload"/);
});

test('a lost response is reported as unknown and not retried', async () => {
    const original = {
        kind: 'erp', scope: 'tenant', name: 'ERP', enabled: true, version: 1,
        actions: ['read', 'manage'], base_connection_id: null,
        config: { provider: 'rfc', ashost: 'h', sysnr: '00', client: '1', user: 'u', lang: 'EN' },
        secrets: {},
    };
    const app = boot({
        responder(url) {
            if (url.indexOf('/types') >= 0) return { json: typesResponse([]) };
            if (url.indexOf('/catalog') >= 0) return { json: okCatalog([]) };
            return { json: { status: 'success' } };
        },
    });
    await app.page.load();
    app.page.openDrawer({ mode: 'edit', kind: 'erp', scope: 'tenant', id: 'c1', version: 1, original });
    app.page.setDraftValue('name', 'Kept name');
    // The transport fails: ecRequest resolves with transport:true.
    app.setResponder(() => { throw new Error('socket closed'); });
    const before = app.fetchCalls.length;
    await app.page.save();
    assert.equal(app.fetchCalls.length, before + 1, 'no silent retry');
    assert.equal(app.page.drawerState().draft.name, 'Kept name');
    assert.equal(app.page.drawerState().unknown, true);
    assert.match(app.document.getElementById('ec-drawer-body').innerHTML, /请求结果未知/);
});

// =====================================================================
// 5. Per-kind forms, the type picker and the operations
// =====================================================================
const ALL_TYPES = [
    MCP_TYPE,
    {
        kind: 'erp', label: 'ERP', config_keys: ['provider'],
        secret_slots: ['password'],
        scopes: [{ scope: 'tenant', available: true, reason: '' }],
        capabilities: {
            open: ['configure'], test_available: false, execute_available: false,
            unavailable_reason: 'awaiting_sap_test_environment', missing_secret_slots: ['password'],
        },
    },
    {
        kind: 'oa', label: 'OA', config_keys: ['base_url'],
        secret_slots: ['password', 'app_secret'],
        scopes: [{ scope: 'tenant', available: false, reason: 'singleton_exists' }],
        capabilities: {
            open: ['configure'], test_available: false, execute_available: false,
            unavailable_reason: 'awaiting_oa_test_environment', missing_secret_slots: [],
        },
    },
    {
        kind: 'email', label: 'Email', config_keys: ['imap', 'smtp'],
        secret_slots: ['imap_password', 'smtp_password'],
        scopes: [{ scope: 'personal', available: false, reason: 'management_required' }],
        capabilities: {
            open: ['configure'], test_available: false, execute_available: false,
            unavailable_reason: 'awaiting_mail_test_environment', missing_secret_slots: [],
        },
    },
];

const envelope = payload => ({ json: { data: payload, request_id: 'req-1' } });
const flush = () => new Promise(resolve => setImmediate(resolve));

test('each kind builds its full field set, and the picker routes to what exists', async () => {
    const app = boot({
        responder(url) {
            if (url.indexOf('/types') >= 0) return { json: typesResponse(ALL_TYPES) };
            if (url.indexOf('/catalog') >= 0) return envelope({ items: [], total: 0, scope: 'tenant' });
            return envelope({});
        },
    });
    await app.page.load();

    const fields = {
        mcp: ['data-ec-field="transport"', 'data-ec-field="url"', 'data-ec-group="mcp-stdio"',
            'data-ec-field="command"', 'data-ec-secret="header"', 'data-ec-secret="env"'],
        erp: ['data-ec-field="provider"', 'data-ec-field="ashost"', 'data-ec-field="client"',
            'data-ec-secret="password"'],
        oa: ['data-ec-field="base_url"', 'data-ec-field="app_key"',
            'data-ec-secret="app_secret"', 'data-ec-secret="password"'],
        email: ['data-ec-field="imap_host"', 'data-ec-field="imap_port"', 'data-ec-field="smtp_host"',
            'data-ec-field="smtp_tls_mode"', 'data-ec-secret="imap_password"', 'data-ec-secret="smtp_password"'],
    };
    Object.keys(fields).forEach(kind => {
        const scope = kind === 'email' ? 'personal' : 'tenant';
        app.page.openDrawer({ mode: 'create', kind, scope, original: {} });
        const form = app.drawerBody().innerHTML;
        assert.ok(form.length > 200, kind + ' renders a form');
        fields[kind].forEach(marker => assert.ok(
            form.indexOf(marker) >= 0, kind + ' must render ' + marker));
        // The whole per-type field set is present in one form; switching
        // transport/auth reveals groups instead of rebuilding and losing input.
        if (kind === 'mcp') {
            assert.ok(form.indexOf('data-ec-group="mcp-remote"') >= 0
                && form.indexOf('data-ec-group="mcp-stdio"') >= 0);
        }
        assert.doesNotMatch(form, /演示|模拟/);
        app.page.closeDrawer(true);
    });

    app.page.openTypePicker();
    const picker = app.panel().innerHTML;
    assert.ok(picker.indexOf('data-ec-action="pick-type" data-kind="mcp"') >= 0,
        'a creatable type offers create');
    assert.ok(picker.indexOf('data-ec-action="pick-type" data-kind="erp"') >= 0);
    assert.equal(picker.indexOf('data-ec-action="pick-type" data-kind="oa"'), -1,
        'a singleton is never offered for creation');
    assert.ok(picker.indexOf('data-ec-action="pick-existing" data-kind="oa"') >= 0);
    assert.ok(picker.indexOf(zh.ec_type_go_edit_existing) >= 0);
    // A type that cannot be created carries the server's own reason sentence.
    assert.ok(picker.indexOf(zh.ec_type_reason_management_required) >= 0);
});

const ERP_DEFAULT_CARD = {
    id: 'erp-1', effective_id: 'erp-1', kind: 'erp', scope: 'tenant', name: 'SAP 生产',
    enabled: true, source: 'tenant', version: 3, actions: ['read', 'manage'],
    test_status: 'untested', tested_at: null, base_connection_id: null,
    config: { provider: 'rfc', ashost: 'h', sysnr: '00', client: '1', user: 'u', lang: 'EN' },
    secrets: { password: { configured: true } },
};
const ERP_SPARE_CARD = {
    id: 'erp-2', effective_id: 'erp-2', kind: 'erp', scope: 'tenant', name: 'SAP 备用',
    enabled: false, source: 'tenant', version: 4, actions: ['read', 'manage'],
    test_status: 'untested', tested_at: null, base_connection_id: null,
    config: { provider: 'rfc', ashost: 'h2', sysnr: '00', client: '1', user: 'u', lang: 'EN' },
    secrets: {},
};
const ERP_THIRD_CARD = {
    id: 'erp-3', effective_id: 'erp-3', kind: 'erp', scope: 'tenant', name: 'SAP 备用二',
    enabled: true, source: 'tenant', version: 9, actions: ['read', 'manage'],
    test_status: 'untested', tested_at: null, base_connection_id: null,
    config: { provider: 'rfc', ashost: 'h3', sysnr: '00', client: '1', user: 'u', lang: 'EN' },
    secrets: {},
};
const INHERITED_CARD = {
    id: 'plat-1', effective_id: 'ovr-9', kind: 'mcp', scope: 'platform', name: '平台 MCP',
    enabled: true, source: 'inherited', version: 5, actions: ['read', 'manage'],
    test_status: 'untested', tested_at: null, base_connection_id: 'ovr-9', config: {},
};

test('operations carry the impact copy and render the server refusal', async () => {
    let deleteAnswer = envelope({});
    const app = boot({
        tenantId: 'tenant-1',
        responder(url, init) {
            const post = !!(init && init.method === 'POST');
            if (url.indexOf('/types') >= 0) return { json: typesResponse(ALL_TYPES) };
            if (url.indexOf('/catalog') >= 0) {
                if (url.indexOf('scope=platform') >= 0) {
                    return envelope({ items: [INHERITED_CARD], total: 1, scope: 'platform' });
                }
                if (url.indexOf('scope=personal') >= 0) return envelope({ items: [], total: 0, scope: 'personal' });
                return envelope({ items: [ERP_DEFAULT_CARD, ERP_SPARE_CARD, ERP_THIRD_CARD], total: 3, scope: 'tenant' });
            }
            if (url.indexOf('/erp-default') >= 0 && !post) {
                return envelope({ connection_id: 'erp-1', revision: 7 });
            }
            if (url.indexOf('/delete') >= 0) return deleteAnswer;
            if (url.indexOf('/tenant-access') >= 0 && !post) {
                return envelope({ tenants: [{ tenant_id: 't1', enabled: true }, { tenant_id: 't2', enabled: false }], revision: 3 });
            }
            return envelope({});
        },
    });
    await app.page.load();
    const cardFor = id => app.page.state.cards.filter(c => c.id === id)[0];
    assert.ok(cardFor('erp-1') && cardFor('erp-2') && cardFor('plat-1'), 'the catalogue loaded');

    assert.ok(app.page.state.scopes.indexOf('tenant') >= 0, 'the tenant range is readable');
    await flush();   // the ERP-default pointer is read after the catalogue lands
    assert.equal(app.page.state.erpDefault && app.page.state.erpDefault.connection_id, 'erp-1');

    // 1. Deleting the ERP default states the impact and asks what happens to it.
    //    The tenant admin picks a replacement, and that choice must reach the
    //    server (it is read before the modal is emptied).
    app.page.confirmDelete(cardFor('erp-1'));
    const confirmCopy = app.panel().innerHTML;
    assert.ok(confirmCopy.indexOf(zh.ec_confirm_delete_title) >= 0);
    assert.ok(confirmCopy.indexOf(ERP_DEFAULT_CARD.name) >= 0);
    assert.ok(confirmCopy.indexOf('data-ec-default-handling') >= 0, 'the default is addressed');
    app.panel().querySelector('[data-ec-default-handling]').querySelector('select').value = 'erp-3';
    app.fire(app.panelEl('[data-ec-action="modal-confirm"]'), 'click');
    await flush();
    let write = lastWrite(app);
    assert.ok(write.url.indexOf('/api/external-connections/tenant/erp-1/delete') >= 0);
    assert.deepEqual(JSON.parse(write.init.body),
        { expected_version: 3, default_handling: { action: 'replace', connection_id: 'erp-3' } });

    // 2. A live reference refuses the delete with the server's summary, once.
    deleteAnswer = {
        status: 409,
        json: {
            error: {
                code: 'referenced', message: 'in use',
                references: [{ kind: 'erp_default' }, { kind: 'override', count: 2 }],
            },
            request_id: 'req-2',
        },
    };
    app.page.confirmDelete(cardFor('erp-1'));
    app.fire(app.panelEl('[data-ec-action="modal-confirm"]'), 'click');
    await flush();
    assert.equal(app.fetchCalls.filter(c => c.url.indexOf('/delete') >= 0).length, 2,
        'a refused delete is not retried automatically');
    const refusal = app.panel().innerHTML;
    assert.ok(refusal.indexOf(zh.ec_referenced_title) >= 0);
    assert.ok(refusal.indexOf(zh.ec_reference_kind_erp_default) >= 0);
    assert.ok(refusal.indexOf(zh.ec_reference_kind_override + ' × 2') >= 0);

    // 3. Enable / disable. Turning one on is immediate; disabling confirms first.
    app.page.toggleConnection(cardFor('erp-2'));
    await flush();
    assert.deepEqual(JSON.parse(lastWrite(app).init.body), { expected_version: 4, enabled: true });
    app.page.toggleConnection(cardFor('erp-1'));
    assert.ok(app.panel().innerHTML.indexOf(zh.ec_confirm_disable_title) >= 0);
    app.fire(app.panelEl('[data-ec-action="modal-confirm"]'), 'click');
    await flush();
    // Disabling the default also says what happens to the pointer.
    assert.deepEqual(JSON.parse(lastWrite(app).init.body),
        { expected_version: 3, enabled: false, default_handling: { action: 'clear' } });

    // 4. The ERP default pointer moves with its CAS revision, and clears explicitly.
    app.page.setErpDefault(cardFor('erp-1'), false);
    app.fire(app.panelEl('[data-ec-action="modal-confirm"]'), 'click');
    await flush();
    write = lastWrite(app);
    assert.ok(write.url.indexOf('/api/external-connections/tenant/erp-default') >= 0);
    assert.deepEqual(JSON.parse(write.init.body), { connection_id: 'erp-1', expected_revision: 7 });
    app.page.setErpDefault(cardFor('erp-1'), true);
    app.fire(app.panelEl('[data-ec-action="modal-confirm"]'), 'click');
    await flush();
    assert.deepEqual(JSON.parse(lastWrite(app).init.body), { connection_id: null, expected_revision: 7 });

    // 5. Dropping a tenant override goes through the platform connection it overrides.
    app.page.restoreInheritance(cardFor('plat-1'));
    app.fire(app.panelEl('[data-ec-action="modal-confirm"]'), 'click');
    await flush();
    write = lastWrite(app);
    assert.ok(write.url.indexOf('/api/external-connections/tenant/ovr-9/restore-inheritance') >= 0);
    assert.deepEqual(JSON.parse(write.init.body), { expected_version: 5 });

    // 6. Tenant access lists only the tenants that are actually enabled, and
    //    saves with the revision the read returned.
    await app.page.openTenantAccess(cardFor('plat-1'));
    await flush();
    const area = app.panelEl('#ec-tenant-access-input');
    assert.equal(area.value, 't1', 'a disabled grant is not presented as access');
    area.value = 't1\nt3';
    app.fire(area, 'input');
    app.fire(app.panelEl('[data-ec-action="tenant-access-save"]'), 'click');
    await flush();
    write = lastWrite(app);
    assert.ok(write.url.indexOf('/api/external-connections/platform/plat-1/tenant-access') >= 0);
    assert.deepEqual(JSON.parse(write.init.body), { tenant_ids: ['t1', 't3'], expected_revision: 3 });
});

// =====================================================================
// 6. The recorded test state reaches the console unchanged
// =====================================================================
// The backend reports a connection's last test result as `test_status` +
// `tested_at` (bound to the config and secret versions it was produced from),
// and the stored test response carries the same pair. The page must render and
// announce *that*, because the failure mode this guards against is a console
// that shows 尚未测试 for a connection that passed a minute ago.
const OPEN_MCP_TYPE = Object.assign({}, MCP_TYPE, {
    scopes: [{ scope: 'platform', available: true, reason: '' },
             { scope: 'tenant', available: true, reason: '' }],
    capabilities: {
        open: ['configure', 'test'], test_available: true, execute_available: false,
        unavailable_reason: '', missing_secret_slots: [],
    },
});

const TESTED_AT = 1790478000;
const PASSED_CARD = {
    id: 'ok-1', effective_id: 'ok-1', kind: 'mcp', scope: 'tenant', name: 'WeKnora MCP',
    enabled: true, source: 'tenant', version: 2, test_status: 'ok', tested_at: TESTED_AT,
    actions: ['read', 'manage'], base_connection_id: null,
};
const UNTESTED_CARD = Object.assign({}, PASSED_CARD, {
    test_status: 'untested', tested_at: null,
});

// The request, the decision and the repaint are more than one microtask turn:
// wait for the card to actually reflect the server's answer rather than betting
// on a number of ticks.
async function settle(predicate) {
    for (let i = 0; i < 20; i += 1) {
        await flush();
        if (!predicate || predicate()) return;
    }
}

// The idiom for "and nothing else happened": run every queued turn, so an
// assertion about a request the page must not send fails on the real attempt
// rather than on a race the test happened to win.
async function drain() {
    for (let i = 0; i < 6; i += 1) await flush();
}

// Every markup the page has put into the catalogue container since `from`.
// "The card never disappeared" is a claim about every render, not about the
// state either side of the request: the loading face is entered in a later turn
// than the click, and the restored list replaces it again.
function rendersSince(root, from) {
    return harness.renders.slice(from)
        .filter(entry => entry.key === root._key)
        .map(entry => entry.html);
}

// The renders of the operation that show the list taken off the screen.
function tornDownFrames(frames) {
    return frames.filter(html => html.indexOf(zh.ec_loading) >= 0);
}

function catalogueApp(card, onTest, scope, type) {
    // `scope` is the scope the listed card lives in; the catalogue only answers
    // for that one, exactly as a caller entitled to a single scope would see.
    const where = scope || 'tenant';
    const state = { card };
    return boot({
        tenantId: 'tenant-1',
        responder(url, init) {
            if (url.indexOf('/types') >= 0) {
                return {
                    json: typesResponse([type || OPEN_MCP_TYPE],
                        [{ scope: 'tenant', available: true }]),
                };
            }
            if (url.indexOf('/catalog') >= 0) {
                const asked = ['tenant', 'personal', 'platform']
                    .find(s => url.indexOf('scope=' + s) >= 0) || 'tenant';
                // Copied, the way a real response is: the page owns the cards it
                // holds, and what it writes to its own state must not reach back
                // into the fixtures the other cases share.
                const items = asked === where ? [Object.assign({}, state.card)] : [];
                return envelope({ items, total: items.length, scope: asked });
            }
            if (url.indexOf('/test') >= 0) {
                if (init && init.method === 'POST') {
                    state.card = onTest.card;
                    // `raw` carries a refusal verbatim (a 409 with its code);
                    // anything else is the recorded result the server returns.
                    return onTest.raw || envelope(onTest.response);
                }
                // The per-connection read the page may fall back to.
                return envelope(onTest.state || { status: 'untested', ran_at: null });
            }
            return envelope({});
        },
    });
}

test('a recorded pass is rendered as the server reported it, not as 未测试', async () => {
    const app = catalogueApp(PASSED_CARD, {});
    await app.page.load();
    await flush();
    const html = app.root().innerHTML;
    assert.ok(html.indexOf(zh.ec_status_ok) >= 0, 'the recorded verdict is shown');
    assert.equal(html.indexOf(zh.ec_never_tested), -1,
        'a connection with a recorded pass is never shown as 尚未测试');
    assert.equal(app.page.state.cards[0].tested_at, TESTED_AT,
        'the recorded time survives the projection');
});

test('a saved test announces the state the server recorded for it', async () => {
    // The server records the pass and reports it in the same response, exactly
    // as `ConnectionRuntime.probe` does once the result is stored.
    const app = catalogueApp(UNTESTED_CARD, {
        response: { outcome: 'ok', recorded: true, stale: false,
                    test_status: 'ok', tested_at: TESTED_AT },
        card: PASSED_CARD,
    });
    await app.page.load();
    await flush();
    assert.equal(app.page.state.cards[0].test_status, 'untested', 'nothing recorded yet');

    app.page.runTest(app.page.state.cards[0]);
    await settle(() => app.page.state.cards[0].test_status === 'ok');
    assert.equal(app.toasts.length, 1, 'the outcome is announced exactly once');
    assert.equal(app.toasts[0][0], zh.ec_status_ok,
        'the toast repeats the recorded verdict, and never invents one');
    const html = app.root().innerHTML;
    assert.ok(html.indexOf(zh.ec_status_ok) >= 0, 'the card shows the recorded pass');
    assert.equal(html.indexOf(zh.ec_never_tested), -1);

    // An expired result is its own state: tested, then edited. Collapsing it
    // back to 尚未测试 is the regression this whole change exists to prevent.
    const expired = catalogueApp(Object.assign({}, PASSED_CARD, { test_status: 'expired' }), {});
    await expired.page.load();
    await flush();
    assert.ok(expired.root().innerHTML.indexOf(zh.ec_status_expired) >= 0);
    assert.equal(expired.root().innerHTML.indexOf(zh.ec_never_tested), -1);
});

// =====================================================================
// 6b. A recorded result lands on the card, and nothing else moves
// =====================================================================
// Answering a test by re-reading the catalogue put the whole list through its
// loading face: the cards vanished, and the page only came back after `/types`
// and every scope's `/catalog` had returned in turn. These cases hold the page
// to the narrower promise the server's response already supports — the one card
// that was tested takes the answer, and the rest of the page is not disturbed.
const RECORDED_PASS = {
    connection_id: 'ok-1', outcome: 'ok', recorded: true, stale: false,
    test_status: 'ok', tested_at: TESTED_AT,
};

test('a test lands on the tested card without tearing the catalogue down', async () => {
    const app = catalogueApp(UNTESTED_CARD, { response: RECORDED_PASS, card: PASSED_CARD });
    await app.page.load();
    await flush();
    assert.equal(app.page.state.cards[0].test_status, 'untested', 'nothing recorded yet');

    const root = app.root();
    const before = app.fetchCalls.length;
    const from = harness.renders.length;
    app.page.runTest(app.page.state.cards[0]);
    await drain();
    // The loading face is what made the card vanish, and the two sequential
    // reads are what made the gap last longer than a frame. Neither may happen:
    // every render of the operation is the catalogue, still on screen.
    const frames = rendersSince(root, from);
    assert.ok(frames.length > 0, 'the card is repainted with the server\'s answer');
    const torn = tornDownFrames(frames);
    assert.equal(torn.length, 0,
        'no render shows the list taken off the screen; first bad render: '
        + String(torn[0] || '').slice(0, 160));

    assert.equal(app.page.state.cards[0].test_status, 'ok', 'the card takes the recorded verdict');
    assert.equal(app.page.state.cards[0].tested_at, TESTED_AT, 'and the moment it was recorded');

    const after = app.fetchCalls.slice(before);
    assert.equal(after.length, 1, 'the test is the only request the operation makes');
    assert.ok(after[0].url.indexOf('/test') >= 0, 'and that request is the test itself');
    assert.ok(app.root().innerHTML.indexOf(zh.ec_status_ok) >= 0, 'the card renders the new verdict');
    assert.equal(app.root().innerHTML.indexOf(zh.ec_never_tested), -1);
});

// =====================================================================
// 6c. Execution availability is its own fact on the card
// (change add-external-connection-agent-assignment: 分配状态与连接测试健康、
//  启停和执行开放状态分别表达; scenario 分配成功但业务执行关闭)
//
// An assignment, a passing test and "the tools can actually be dispatched" are
// three different statements, and the card used to make only the first two:
// `ecTestStateHtml` reads `cap.test_available` and nothing ever read the
// execution classes. So a card could say 已配置 · 1 个智能体可用 next to a
// green 连接正常 while every tool call was still refused — which is exactly
// what "分配成功但业务执行关闭" forbids. These cases pin the fourth fact.
function execCard(overrides) {
    return assignCard(Object.assign({
        test_status: 'ok', tested_at: TESTED_AT,
        agent_assignment: {
            configured: true, revision: 2, visible_count: 1, can_assign: true,
        },
    }, overrides || {}));
}

function execType(overrides) {
    return Object.assign({}, OPEN_MCP_TYPE, {
        capabilities: Object.assign({}, OPEN_MCP_TYPE.capabilities, overrides || {}),
    });
}

// The deployment as it really is: MCP can be configured and tested, and every
// execution class is closed for its own reason.
const EXEC_CLOSED = {
    execute_available: false,
    read_execute_available: false,
    write_execute_available: false,
    unavailable_reason: 'awaiting_mcp_test_environment',
    classes: {
        configure: { available: true, reason: '' },
        test: { available: true, reason: '' },
        read_execute: { available: false, reason: 'awaiting_mcp_test_environment' },
        write_execute: { available: false, reason: 'not_supported_by_type' },
    },
};
const EXEC_READ_ONLY = Object.assign({}, EXEC_CLOSED, {
    execute_available: true,
    read_execute_available: true,
    classes: Object.assign({}, EXEC_CLOSED.classes, {
        read_execute: { available: true, reason: '' },
    }),
});
const EXEC_READ_WRITE = Object.assign({}, EXEC_READ_ONLY, {
    write_execute_available: true,
    classes: Object.assign({}, EXEC_READ_ONLY.classes, {
        write_execute: { available: true, reason: '' },
    }),
});
// Reads closed while writes stay open: not a state any kind ships today, but
// `OPENABLE_CLASSES` is per kind and per deployment, so the renderer must not
// assume the read class is the one that opens first.
const EXEC_WRITE_ONLY = Object.assign({}, EXEC_READ_WRITE, {
    read_execute_available: false,
    classes: Object.assign({}, EXEC_READ_WRITE.classes, {
        read_execute: { available: false, reason: 'withheld_pending_acceptance' },
    }),
});

test('an assigned connection whose execution is closed says so beside the assignment', async () => {
    const app = catalogueApp(execCard(), {}, 'tenant', execType(EXEC_CLOSED));
    await app.page.load();
    await flush();

    const html = app.root().innerHTML;
    assert.ok(zh.ec_card_exec_closed, 'the closed-execution label has a translation');
    assert.ok(html.indexOf(zh.ec_assign_summary_configured.replace('{n}', '1')) >= 0,
        'the assignment is still reported: it did succeed');
    assert.ok(html.indexOf(zh.ec_card_exec_closed) >= 0,
        'and the closed execution is stated as its own fact');
    assert.ok(html.indexOf(zh.ec_reason_awaiting_mcp_test_environment) >= 0,
        "with the server's own reason, not a placeholder");
    assert.equal(html.indexOf(zh.ec_card_exec_read_only), -1,
        'nothing claims the tools can be dispatched');
    assert.equal(html.indexOf(zh.ec_card_exec_read_write), -1,
        'and nothing claims writes either');
    // The reason sentence must not close the *test* class: on this very card the
    // test class is open, so 测试保持关闭 would contradict the row below it.
    assert.equal(zh.ec_reason_awaiting_mcp_test_environment.indexOf('测试保持关闭'), -1,
        'the reason states a cause, not which class is closed');
});

test('only the classes a deployment really opened are reported open', async () => {
    // Each branch is a different promise and the card must not blur them: a
    // read slice is not a write slice, and a half-open pair is not a closed one.
    const cases = [
        { name: 'reads only', cap: EXEC_READ_ONLY, key: 'ec_card_exec_read_only', reason: true },
        { name: 'writes only', cap: EXEC_WRITE_ONLY, key: 'ec_card_exec_write_only', reason: true },
        { name: 'reads and writes', cap: EXEC_READ_WRITE, key: 'ec_card_exec_read_write', reason: false },
    ];
    for (const c of cases) {
        const app = catalogueApp(execCard(), {}, 'tenant', execType(c.cap));
        await app.page.load();
        await flush();

        const html = app.root().innerHTML;
        assert.ok(zh[c.key], c.name + ': the label has a translation');
        assert.ok(html.indexOf(zh[c.key]) >= 0, c.name + ': the card states it');
        for (const other of ['ec_card_exec_closed', 'ec_card_exec_read_only',
            'ec_card_exec_write_only', 'ec_card_exec_read_write']) {
            if (other === c.key) continue;
            assert.equal(html.indexOf(zh[other]), -1,
                c.name + ': nothing also claims ' + other);
        }
        if (c.key === 'ec_card_exec_read_write') {
            assert.equal(c.reason, false, 'a fully open pair has no reason to give');
        }
    }
});

test('a card with no type projection claims nothing about execution', async () => {
    // The projection is the only evidence for this line. Without one for the
    // card's kind there is nothing truthful to say, and guessing 执行开放 from
    // the mere existence of a card is the claim this whole requirement bans.
    const app = catalogueApp(execCard(), {}, 'tenant', {
        kind: 'erp', label: 'ERP', label_key: 'ext_conn_type_erp',
        config_keys: [], secret_slots: [],
        scopes: [{ scope: 'tenant', available: true, reason: '' }],
        capabilities: { test_available: true, execute_available: true, unavailable_reason: '' },
    });
    await app.page.load();
    await flush();

    const html = app.root().innerHTML;
    // Liveness first: absences below only mean something if the card is really
    // on screen and its assignment is being reported.
    assert.ok(html.indexOf(zh.ec_assign_summary_configured.replace('{n}', '1')) >= 0,
        'the card is rendered with its assignment summary');
    assert.equal(html.indexOf(zh.ec_card_exec_closed), -1);
    assert.equal(html.indexOf(zh.ec_card_exec_read_only), -1);
    assert.equal(html.indexOf(zh.ec_card_exec_write_only), -1);
    assert.equal(html.indexOf(zh.ec_card_exec_read_write), -1);
});

test('a verdict for a row the card does not render is not written onto it', async () => {
    // An overridden platform template. The card is addressed by the template's
    // id — that is the row the button tests — while the tenant's override is
    // what a scene would actually run, and the card renders the override. A
    // fresh verdict for the template says nothing about the configuration in
    // force, so writing it would describe a connection that will never run.
    const overridden = Object.assign({}, UNTESTED_CARD, {
        id: 'tpl-1', effective_id: 'ovr-1', scope: 'platform', source: 'overridden',
    });
    const app = catalogueApp(overridden, {
        response: Object.assign({}, RECORDED_PASS, { connection_id: 'tpl-1' }),
        card: overridden,
    }, 'platform');
    await app.page.load();
    await flush();

    app.page.runTest(app.page.state.cards[0]);
    await drain();
    assert.equal(app.toasts.length, 1, 'the operator is still told what was tested');
    assert.equal(app.toasts[0][0], zh.ec_status_ok);
    assert.equal(app.page.state.cards[0].test_status, 'untested',
        "the template's verdict is not shown as the override's state");
    assert.equal(app.page.state.cards[0].tested_at, null, 'nor its time');
    assert.ok(app.root().innerHTML.indexOf(zh.ec_never_tested) >= 0);
});

test('a discarded result re-reads the one connection instead of the catalogue', async () => {
    // The configuration moved while the probe ran, so the server recorded
    // nothing and said so. Leaving the previous verdict on screen would keep
    // showing a state the server has already voided.
    const app = catalogueApp(PASSED_CARD, {
        raw: {
            status: 409,
            json: { status: 'error', code: 'test_result_stale',
                    message: 'connection changed while the test ran' },
        },
        state: { status: 'expired', ran_at: TESTED_AT + 60 },
        card: PASSED_CARD,
    });
    await app.page.load();
    await flush();
    assert.equal(app.page.state.cards[0].test_status, 'ok', 'the old reading is on screen');

    const root = app.root();
    const before = app.fetchCalls.length;
    const from = harness.renders.length;
    app.page.runTest(app.page.state.cards[0]);
    await drain();
    assert.equal(tornDownFrames(rendersSince(root, from)).length, 0,
        'a refusal is not a reason to take the list off the screen either');

    const after = app.fetchCalls.slice(before);
    assert.equal(after.length, 2, 'the test, then exactly one re-read of that connection');
    assert.equal(after[0].init.method, 'POST', 'the test itself');
    assert.equal(after[1].init.method, 'GET', 'a read, not another test');
    assert.ok(after[1].url.indexOf('/test') >= 0, 'the read is that connection\'s state');
    assert.equal(after.filter(c => c.url.indexOf('/catalog') >= 0).length, 0, 'no catalogue read');
    assert.equal(after.filter(c => c.url.indexOf('/types') >= 0).length, 0, 'no type read');

    assert.equal(app.page.state.cards[0].test_status, 'expired',
        'the discarded verdict is replaced by what the server now holds');
    assert.equal(app.page.state.cards[0].tested_at, TESTED_AT + 60);
    assert.equal(app.toasts[0][1], 'error', 'the refusal is reported, not hidden');
    const html = app.root().innerHTML;
    assert.ok(html.indexOf(zh.ec_status_expired) >= 0);
    assert.equal(html.indexOf(zh.ec_status_ok), -1, 'the voided verdict is off the card');
});

test('the test control keeps the keyboard position after the card is repainted', async () => {
    const app = catalogueApp(UNTESTED_CARD, { response: RECORDED_PASS, card: PASSED_CARD });
    await app.page.load();
    await flush();

    // The control the user pressed holds focus when the request goes out, and
    // the repaint that follows replaces the node it was on.
    app.testControl('ok-1').focus();

    app.page.runTest(app.page.state.cards[0]);
    await settle(() => app.page.state.cards[0].test_status === 'ok');
    const landed = harness.focused[harness.focused.length - 1];
    assert.equal(landed, app.testControl('ok-1'),
        'focus comes back to the same control, so a keyboard user is not sent looking for it');
});

test('a repaint does not take focus from wherever the user moved on to', async () => {
    const app = catalogueApp(UNTESTED_CARD, { response: RECORDED_PASS, card: PASSED_CARD });
    await app.page.load();
    await flush();

    app.testControl('ok-1').focus();
    app.page.runTest(app.page.state.cards[0]);
    // The probe is still running and the user has already gone somewhere else.
    const elsewhere = app.root().querySelector('[data-ec-action="add"]');
    assert.ok(elsewhere, 'the add control is on the page');
    elsewhere.focus();

    await settle(() => app.page.state.cards[0].test_status === 'ok');
    const landed = harness.focused[harness.focused.length - 1];
    assert.ok(landed === elsewhere, 'the user keeps the position they chose');
});

test('a result arriving under a new identity is dropped', async () => {
    const app = catalogueApp(UNTESTED_CARD, { response: RECORDED_PASS, card: UNTESTED_CARD });
    await app.page.load();
    await flush();

    app.page.runTest(app.page.state.cards[0]);
    // The session was re-authenticated while the probe ran.
    app.ctx._authEpoch = 7;
    await drain();

    assert.equal(app.page.state.cards[0].test_status, 'untested',
        'an answer owed to the previous identity is not written onto the page');
    assert.equal(app.page.state.cards[0].tested_at, null);
});

// =====================================================================
// 7. Accessibility / layout conventions
// =====================================================================
test('the page keeps 390px, large-font and theme conventions', () => {
    assert.match(css, /@media \(max-width: 420px\)/);
    assert.match(css, /\.ec-drawer-panel \{ width: 100%; \}/);
    // Grids collapse instead of overflowing.
    assert.match(css, /minmax\(min\(22rem, 100%\), 1fr\)/);
    assert.match(css, /minmax\(min\(13rem, 100%\), 1fr\)/);
    // Dark theme coverage for the page surfaces.
    assert.match(css, /\.dark \.ec-card \{/);
    assert.match(css, /\.dark \.ec-drawer-panel \{/);
    // Keyboard reachability: focus rings on the interactive controls.
    assert.match(css, /\.ec-btn:focus-visible/);
    assert.match(css, /\.ec-chip:focus-visible/);
    assert.match(css, /\.ec-input:focus/);
    // Reduced-motion-free, but the drawer is a dialog with a focus trap.
    assert.match(pageJs, /setAttribute\('role', 'dialog'\)/);
    assert.match(pageJs, /function ecTrapFocus/);
    assert.match(pageJs, /function ecRestoreFocus/);
    assert.match(css, /\.ec-assign-row/);
    assert.match(css, /\.dark \.ec-assign-row/);
});

// =====================================================================
// 8. Per-connection Agent assignment
// (change add-external-connection-agent-assignment, task group 4)
//
// The relation is edited as a delta over drafts, so the promises asserted here
// are about *what is not sent* as much as what is: no whole-roster download to
// search locally, no page-or-visible-set replacement on save, no silent
// re-send after a 409 or a lost response, and no "已添加" that could be added
// twice because the assignment lives on a page the caller never loaded.
// =====================================================================
function assignCard(overrides) {
    const base = {
        id: 'conn-a1', effective_id: 'conn-a1', kind: 'mcp', scope: 'tenant',
        name: '团队 MCP', enabled: true, source: 'created', version: 1,
        test_status: 'untested', tested_at: null, actions: ['read', 'manage'],
        base_connection_id: null,
        agent_assignment: { configured: true, revision: 1, visible_count: 0, can_assign: true },
    };
    const merged = Object.assign({}, base, overrides || {});
    merged.agent_assignment = Object.assign({}, base.agent_assignment,
        (overrides && overrides.agent_assignment) || {});
    return merged;
}

function agentItem(id, name, overrides) {
    return Object.assign({
        id, name: name || id, category: '', enabled: true,
        visibility: 'tenant', assigned: false, manageable: true,
    }, overrides || {});
}

function assignPage(items, total, revision, extra) {
    return Object.assign({
        items, total: total === undefined ? items.length : total,
        page: 1, page_size: 20, has_more: false,
        configured: true, revision, can_assign: true,
    }, extra || {});
}

// A catalogue card is serialized on the wire, so the page owns its own copy:
// what it writes to its state must not reach back into the fixture.
function assignsPage(state, options) {
    const opts = options || {};
    return boot({
        tenantId: 'tenant-1',
        responder(url, init) {
            if (url.indexOf('/types') >= 0) {
                return { json: typesResponse([OPEN_MCP_TYPE], [{ scope: 'tenant', available: true }]) };
            }
            if (url.indexOf('/catalog') >= 0) {
                const asked = ['platform', 'personal', 'tenant']
                    .find(scope => url.indexOf('scope=' + scope) >= 0) || 'tenant';
                const items = asked === 'tenant' ? [JSON.parse(JSON.stringify(state.card))] : [];
                return envelope({ items, total: items.length, scope: asked });
            }
            if (url.indexOf('/agent-candidates') >= 0) {
                if (opts.candidates) return opts.candidates(url);
                return envelope(assignPage([], 0, state.card.agent_assignment.revision, {
                    configured: state.card.agent_assignment.configured,
                    can_assign: state.card.agent_assignment.can_assign,
                }));
            }
            if (url.indexOf('/agent-assignments') >= 0) {
                if (init && init.method === 'POST') {
                    state.posts = (state.posts || []).concat([JSON.parse(init.body)]);
                    return opts.save ? opts.save(state) : envelope({
                        configured: true, revision: 2, added: 0, removed: 0, unchanged: false,
                    });
                }
                state.reads = (state.reads || []).concat([url]);
                if (opts.read) return opts.read(url);
                return envelope(assignPage([], 0, state.card.agent_assignment.revision, {
                    configured: state.card.agent_assignment.configured,
                    can_assign: state.card.agent_assignment.can_assign,
                }));
            }
            return envelope({});
        },
    });
}

const assignmentWrites = app => app.fetchCalls.filter(
    call => call.url.indexOf('/agent-assignments') >= 0 && call.init && call.init.method === 'POST');

// The page runs in its own vm realm, so its arrays are not this test's arrays:
// their contents are what the assertions are about.
const drafted = list => Array.from(list || []);

async function openAssignments(app, card) {
    app.page.openAgentAssignments(card || app.page.state.cards[0]);
    await flush();
    await flush();
}

test('a tenant card offers the assignment entry and states the current regime', () => {
    const app = boot();
    // 已配置 and 未配置 are different promises: one counts the allowed agents,
    // the other says the old permissions still apply.
    const configured = assignCard({
        agent_assignment: { configured: true, revision: 4, visible_count: 3, can_assign: true },
    });
    assert.match(app.page.cardHtml(configured), /data-ec-action="agents"/);
    assert.ok(app.page.cardHtml(configured).indexOf(zh.ec_assign_summary_configured.replace('{n}', '3')) >= 0,
        'a configured connection counts its allowed agents');

    const fresh = assignCard({
        agent_assignment: { configured: false, revision: 0, visible_count: 0, can_assign: true },
    });
    assert.ok(app.page.cardHtml(fresh).indexOf(zh.ec_assign_summary_unconfigured) >= 0,
        'an unconfigured connection says the old permissions still apply');

    // 平台目录没有业务租户: without a tenant relation there is nothing to manage,
    // so no entry and no regime summary are invented for the card.
    const platform = assignCard({ scope: 'platform' });
    delete platform.agent_assignment;
    const html = app.page.cardHtml(platform);
    assert.equal(html.indexOf('data-ec-action="agents"'), -1);
    assert.equal(html.indexOf('data-ec-assign-summary'), -1);
});

test('opening the modal loads the assigned page only, never the whole roster', async () => {
    const state = { card: assignCard({ agent_assignment: { visible_count: 2 } }) };
    const app = assignsPage(state, {
        read: () => envelope(assignPage([
            agentItem('a1', '阿尔法'), agentItem('a2', '贝塔', { visibility: 'private' }),
        ], 2, 1)),
    });
    await app.page.load();
    await openAssignments(app);
    const panel = app.panel().innerHTML;
    assert.ok(panel.indexOf(zh.ec_assign_assigned_title.replace('{n}', '2')) >= 0);
    assert.ok(panel.indexOf('阿尔法') >= 0 && panel.indexOf('贝塔') >= 0);
    assert.ok(panel.indexOf(zh.ec_assign_private) >= 0, 'a private agent is labelled');
    assert.ok(panel.indexOf('data-ec-agents-remove="a1"') >= 0);
    // 空关键词 MUST NOT 加载候选全集 — and the page must not ask for one to find out.
    assert.equal(state.reads.length, 1, 'one read: the assigned first page');
    assert.equal(app.fetchCalls.filter(c => c.url.indexOf('/agent-candidates') >= 0).length, 0,
        'no candidate request without a search term');
    assert.equal(state.reads[0].indexOf('page=1&page_size=20') >= 0, true);
    assert.equal(state.reads[0].indexOf('q='), -1);
});

test('a search is debounced, and an emptied term loads nothing', async () => {
    const state = { card: assignCard() };
    const app = assignsPage(state, {
        read: () => envelope(assignPage([agentItem('a1', '阿尔法')], 1, 1)),
        candidates: () => envelope(assignPage([agentItem('c1', 'Cand One')], 1, 1)),
    });
    await app.page.load();
    await openAssignments(app);

    const search = app.panelEl('[data-ec-agents-search]');
    search.value = 'can';
    app.fire(search, 'input');
    // The debounce is the promise: typing alone asks for nothing yet.
    assert.equal(app.fetchCalls.filter(c => c.url.indexOf('/agent-candidates') >= 0).length, 0);
    assert.equal(app.pendingTimers(), 1);
    app.runTimers();
    await flush();
    const asked = app.fetchCalls.filter(c => c.url.indexOf('/agent-candidates') >= 0);
    assert.equal(asked.length, 1, 'one request for one settled term');
    assert.ok(asked[0].url.indexOf('q=can') >= 0);
    assert.ok(app.panel().innerHTML.indexOf('Cand One') >= 0);

    // Clearing the term drops the candidates without asking the server what it
    // already knows: there is nothing to search for.
    const cleared = app.panelEl('[data-ec-agents-search]');
    cleared.value = '';
    app.fire(cleared, 'input');
    await drain();
    assert.equal(app.fetchCalls.filter(c => c.url.indexOf('/agent-candidates') >= 0).length, 1);
    assert.equal(app.panel().innerHTML.indexOf('Cand One'), -1);
    assert.ok(app.panel().innerHTML.indexOf(zh.ec_assign_search_hint) >= 0);
});

test('an IME composition is not repainted away, and only the committed text searches', async () => {
    // 中文输入法先往框里写预编辑文本（拼音），提交后才是一次真正的输入。组合
    // 期间既不能把拼音当成查询，也不能重绘输入框 —— 重建正在被输入法书写的节点
    // 会丢掉未提交的字符，用户看到的就是「打不出中文」。
    const state = { card: assignCard() };
    const app = assignsPage(state, {
        read: () => envelope(assignPage([agentItem('a1', '阿尔法')], 1, 1)),
        candidates: () => envelope(assignPage([agentItem('c1', '合同审阅助手')], 1, 1)),
    });
    await app.page.load();
    await openAssignments(app);

    const box = app.panelEl('[data-ec-agents-search]');
    app.fire(box, 'compositionstart');
    box.value = 'h';
    app.fire(box, 'input', { isComposing: true });
    box.value = 'hetong';
    app.fire(box, 'input', { isComposing: true });

    assert.equal(app.pendingTimers(), 0, '拼音不触发防抖搜索');
    assert.equal(app.fetchCalls.filter(c => c.url.indexOf('/agent-candidates') >= 0).length, 0,
        '预编辑文本不是查询');
    assert.equal(app.panelEl('[data-ec-agents-search]'), box,
        '组合未提交时输入框不得被重建');

    // 提交：这次的文本才是查询，而且只问一次。
    box.value = '合同';
    app.fire(box, 'compositionend');
    app.runTimers();
    await flush();
    const asked = app.fetchCalls.filter(c => c.url.indexOf('/agent-candidates') >= 0);
    assert.equal(asked.length, 1, '提交后只发一次候选请求');
    assert.ok(asked[0].url.indexOf('q=' + encodeURIComponent('合同')) >= 0,
        '请求带上提交后的中文');
    assert.ok(app.panel().innerHTML.indexOf('合同审阅助手') >= 0);

    // 浏览器在 compositionend 之后还会补一个普通 input 事件：同一个词不重复请求。
    const after = app.panelEl('[data-ec-agents-search]');
    assert.equal(after.value, '合同', '提交后的文本留在框里');
    app.fire(after, 'input', { isComposing: false });
    app.runTimers();
    await drain();
    assert.equal(app.fetchCalls.filter(c => c.url.indexOf('/agent-candidates') >= 0).length, 1,
        '同一个词不重复询问服务器');
});

test('a repaint asked for during a composition is deferred, not dropped', async () => {
    const state = { card: assignCard() };
    let releaseRead;
    const pending = new Promise(resolve => { releaseRead = resolve; });
    const app = assignsPage(state, { read: () => ({ json: pending }) });
    await app.page.load();
    await openAssignments(app);   // 已分配列表的读取还在路上

    const box = app.panelEl('[data-ec-agents-search]');
    app.fire(box, 'compositionstart');
    box.value = 'zhi';
    app.fire(box, 'input', { isComposing: true });

    // 迟到的响应要求一次重绘：组合期间必须推迟，不能让输入框消失。
    releaseRead({ data: assignPage([agentItem('a1', '阿尔法')], 1, 1) });
    await drain();
    assert.equal(app.panelEl('[data-ec-agents-search]'), box,
        '组合未提交时输入框不得被重建');
    assert.equal(app.panel().innerHTML.indexOf('阿尔法'), -1, '重绘被推迟到提交');

    box.value = '知识';
    app.fire(box, 'compositionend');
    await drain();
    assert.ok(app.panel().innerHTML.indexOf('阿尔法') >= 0, '提交后补做被推迟的重绘');
});

test('the catalogue search box survives a composition too', async () => {
    // 目录搜索框每敲一个字就重绘整页 —— 同一个输入法问题，同一个修法：组合期间
    // 不筛选、不重绘，提交后才按提交文本筛选。
    const app = boot({
        responder(url) {
            if (url.indexOf('/types') >= 0) return { json: typesResponse([OPEN_MCP_TYPE]) };
            if (url.indexOf('/catalog') >= 0) {
                return envelope({ items: [JSON.parse(JSON.stringify(MCP_CARD))], total: 1,
                    scope: 'tenant' });
            }
            return envelope({});
        },
    });
    await app.page.load();
    // The catalogue's handlers are delegated from the page root, which is what
    // `mount` wires up; `load` alone would leave the box silently inert.
    app.page.mount();
    await drain();
    assert.ok(tagMarkup(app.root().innerHTML, '[data-ec-action="search"]'),
        '目录渲染出自己的搜索框');

    // The box is a child of the page root and its events are delegated there, so
    // the event carries the box as its target.
    const target = { value: '',
        getAttribute: name => (name === 'data-ec-action' ? 'search' : null) };
    const typed = value => { target.value = value; app.fire(app.root(), 'input', { target }); };
    const painted = () => app.harness.renders.length;

    app.fire(app.root(), 'compositionstart', { target });
    const before = painted();
    typed('oneagent');   // 拼音
    assert.equal(app.page.state.search, '', '组合中的拼音不作为筛选条件');
    assert.equal(painted(), before, '组合期间目录不被重绘');

    target.value = 'OneAgent';
    app.fire(app.root(), 'compositionend', { target });
    assert.equal(app.page.state.search, 'OneAgent', '提交后的文本才生效');
    assert.equal(painted() > before, true, '提交后补做被推迟的重绘');
});

test('a late candidate response cannot overwrite a newer search', async () => {
    let releaseSlow;
    const slow = new Promise(resolve => { releaseSlow = resolve; });
    const state = { card: assignCard() };
    let call = 0;
    const app = assignsPage(state, {
        read: () => envelope(assignPage([], 0, 1)),
        candidates: () => {
            call += 1;
            if (call === 1) return { json: slow };
            return envelope(assignPage([agentItem('fast', 'Fast Result')], 1, 1));
        },
    });
    await app.page.load();
    await openAssignments(app);

    const search = app.panelEl('[data-ec-agents-search]');
    search.value = 'first';
    app.fire(search, 'input');
    app.runTimers();
    const second = app.panelEl('[data-ec-agents-search]');
    second.value = 'second';
    app.fire(second, 'input');
    app.runTimers();
    await flush();
    assert.ok(app.panel().innerHTML.indexOf('Fast Result') >= 0);

    // The first request answers now, long after it was superseded.
    releaseSlow({ data: assignPage([agentItem('stale', 'Stale Result')], 1, 1), request_id: 'r' });
    await drain();
    assert.equal(app.panel().innerHTML.indexOf('Stale Result'), -1,
        'an old response must not overwrite the newer query');
    assert.ok(app.panel().innerHTML.indexOf('Fast Result') >= 0);
});

test('drafts survive search and paging, opposite actions cancel, and only deltas save', async () => {
    const state = { card: assignCard({ agent_assignment: { visible_count: 25 } }) };
    const app = assignsPage(state, {
        read(url) {
            if (url.indexOf('page=2') >= 0) {
                return envelope(assignPage([agentItem('a2', '贝塔')], 25, 1, { page: 2 }));
            }
            return envelope(assignPage([agentItem('a1', '阿尔法')], 25, 1, { has_more: true }));
        },
        candidates: () => envelope(assignPage([
            agentItem('c1', 'Gamma'), agentItem('c2', 'Delta', { assigned: true }),
        ], 2, 1)),
        save: () => envelope({ configured: true, revision: 2, added: 1, removed: 1, unchanged: false }),
    });
    await app.page.load();
    await openAssignments(app);
    assert.equal(app.page.assignmentState().assigned.page, 1);

    // 1. Draft a removal on page 1.
    app.fire(app.panelEl('[data-ec-agents-remove="a1"]'), 'click');
    assert.deepEqual(drafted(app.page.assignmentState().remove), ['a1']);
    assert.ok(app.panel().innerHTML.indexOf('data-ec-agents-remove="a1"') >= 0);
    assert.ok(app.panel().innerHTML.indexOf(zh.ec_assign_undo_remove) >= 0);

    // 2. Search and draft an addition. An agent the server already has
    //    assigned (even though it is not on the loaded page) says 已添加 and
    //    offers no way to add it twice.
    const search = app.panelEl('[data-ec-agents-search]');
    search.value = 'gam';
    app.fire(search, 'input');
    app.runTimers();
    await flush();
    assert.ok(app.panel().innerHTML.indexOf('data-ec-agents-added="c2"') >= 0);
    app.fire(app.panelEl('[data-ec-agents-add="c1"]'), 'click');
    assert.deepEqual(drafted(app.page.assignmentState().add), ['c1']);

    // 3. Page the assigned list: the drafts are not on screen any more, and
    //    they must still be there.
    app.fire(app.panelEl('[data-ec-agents-next="assigned"]'), 'click');
    await flush();
    assert.deepEqual(drafted(app.page.assignmentState().remove), ['a1'], 'paging loses no draft');
    assert.deepEqual(drafted(app.page.assignmentState().add), ['c1']);
    assert.ok(app.panel().innerHTML.indexOf(zh.ec_assign_pending.replace('{changes}', '')) >= 0);

    // 4. 相反操作抵消: the same control that drafted the addition undoes it, and
    //    a draft that cancels out leaves nothing to save.
    app.fire(app.panelEl('[data-ec-agents-add="c1"]'), 'click');
    assert.deepEqual(drafted(app.page.assignmentState().add), [], 'a repeated action cancels the draft');
    app.fire(app.panelEl('[data-ec-agents-add="c1"]'), 'click');
    assert.deepEqual(drafted(app.page.assignmentState().add), ['c1'], 'and it can be drafted again');

    // 5. The save carries the drafts and the revision — never the page, never
    //    the visible set, and it does not delete an agent.
    app.fire(app.panelEl('[data-ec-agents-save]'), 'click');
    await flush();
    assert.equal(state.posts.length, 1);
    assert.deepEqual(state.posts[0], {
        expected_revision: 1, add_agent_ids: ['c1'], remove_agent_ids: ['a1'],
    });
    assert.deepEqual(drafted(app.page.assignmentState().add), [], 'a successful save clears the drafts');
    assert.deepEqual(drafted(app.page.assignmentState().remove), []);
});

test('a 409 keeps the draft, is not re-sent, and a re-read re-arms the save', async () => {
    const state = { card: assignCard() };
    let revision = 1;
    let conflict = true;
    const app = assignsPage(state, {
        read: () => envelope(assignPage([agentItem('a1', '阿尔法')], 1, revision)),
        save: () => {
            if (conflict) {
                return {
                    status: 409,
                    json: { error: { code: 'assignment_version_conflict', message: 'changed' }, request_id: 'r' },
                };
            }
            revision += 1;
            return envelope({ configured: true, revision, added: 0, removed: 1, unchanged: false });
        },
    });
    await app.page.load();
    await openAssignments(app);
    app.fire(app.panelEl('[data-ec-agents-remove="a1"]'), 'click');
    app.fire(app.panelEl('[data-ec-agents-save]'), 'click');
    await flush();

    assert.equal(assignmentWrites(app).length, 1, 'a conflict is not retried');
    assert.ok(app.panel().innerHTML.indexOf(zh.ec_assign_conflict) >= 0);
    assert.ok(app.panel().innerHTML.indexOf('data-ec-agents-reload') >= 0);
    assert.deepEqual(drafted(app.page.assignmentState().remove), ['a1'], 'the draft survives the conflict');
    assert.equal(app.page.assignmentState().conflict, true);

    // Pressing save again before re-reading sends nothing: the stale revision
    // would only race the newer state.
    app.fire(app.panelEl('[data-ec-agents-save]'), 'click');
    await drain();
    assert.equal(assignmentWrites(app).length, 1);

    // 重新读取 picks up the server's current revision and re-arms the save.
    revision = 5;
    conflict = false;
    app.fire(app.panelEl('[data-ec-agents-reload]'), 'click');
    await flush();
    assert.equal(app.page.assignmentState().conflict, false);
    assert.equal(app.page.assignmentState().revision, 5);
    assert.deepEqual(drafted(app.page.assignmentState().remove), ['a1'], 'the draft is still the user\'s');
    app.fire(app.panelEl('[data-ec-agents-save]'), 'click');
    await flush();
    assert.equal(assignmentWrites(app).length, 2);
    assert.equal(assignmentWrites(app)[1].init.body.indexOf('"expected_revision":5') >= 0, true);
});

test('a lost save response is reported as unknown and never re-sent', async () => {
    const state = { card: assignCard() };
    const app = assignsPage(state, {
        read: () => envelope(assignPage([agentItem('a1', '阿尔法')], 1, 1)),
        save: () => { throw new Error('socket closed'); },
    });
    await app.page.load();
    await openAssignments(app);
    app.fire(app.panelEl('[data-ec-agents-remove="a1"]'), 'click');
    app.fire(app.panelEl('[data-ec-agents-save]'), 'click');
    await flush();

    assert.equal(assignmentWrites(app).length, 1, 'no silent retry');
    assert.ok(app.panel().innerHTML.indexOf(zh.ec_assign_unknown) >= 0);
    assert.ok(app.panel().innerHTML.indexOf('data-ec-agents-reload') >= 0);
    assert.deepEqual(drafted(app.page.assignmentState().remove), ['a1']);
    assert.equal(app.toasts.some(([message]) => message === zh.ec_toast_agents_unknown), true,
        'the user is told the outcome is unknown, not that it failed');
});

test('the first save may be empty, and the card then states the configured-empty regime', async () => {
    const state = { card: assignCard({ agent_assignment: { configured: false, revision: 0 } }) };
    let reads = 0;
    const app = assignsPage(state, {
        read: () => {
            reads += 1;
            // The open read still says 未配置; the read after the save sees the
            // configured-empty state the save itself created.
            return envelope(assignPage([], 0, reads > 1 ? 1 : 0, { configured: reads > 1 }));
        },
        save: () => envelope({ configured: true, revision: 1, added: 0, removed: 0, unchanged: false }),
    });
    await app.page.load();
    await openAssignments(app);
    const panel = app.panel().innerHTML;
    assert.ok(panel.indexOf(zh.ec_assign_first_title) >= 0, 'the impact of the first save is stated');
    assert.ok(panel.indexOf(zh.ec_assign_pending_empty) >= 0, 'an empty first save is a real choice');
    assert.equal(app.page.assignmentState().configured, false);

    app.fire(app.panelEl('[data-ec-agents-save]'), 'click');
    await flush();
    assert.equal(state.posts.length, 1);
    assert.deepEqual(state.posts[0], {
        expected_revision: 0, add_agent_ids: [], remove_agent_ids: [],
    });
    assert.equal(reads >= 2, true, 'the list is re-read after the save');
    assert.equal(app.page.state.cards[0].agent_assignment.configured, true);
    assert.ok(app.root().innerHTML.indexOf(
        zh.ec_assign_summary_configured.replace('{n}', '0')) >= 0,
    'the card now states 已配置 empty, not 沿用原权限');
});

test('a save made while searching still updates the card from the server count', async () => {
    // The search box filters the candidates, not the relation, so the card's
    // number is a fact about the connection. A term left in the box must not
    // freeze the summary at its pre-save value (task 4.2).
    const state = { card: assignCard({ agent_assignment: { visible_count: 2 } }) };
    let reads = 0;
    const app = assignsPage(state, {
        read: () => {
            reads += 1;
            return envelope(assignPage([
                agentItem('a1', '阿尔法'), agentItem('a2', '贝塔'),
            ].concat(reads > 1 ? [agentItem('c1', 'Gamma')] : []), reads > 1 ? 3 : 2, 1));
        },
        candidates: () => envelope(assignPage([agentItem('c1', 'Gamma')], 1, 1)),
        save: () => envelope({ configured: true, revision: 2, added: 1, removed: 0, unchanged: false }),
    });
    await app.page.load();
    await openAssignments(app);

    const search = app.panelEl('[data-ec-agents-search]');
    search.value = 'gam';
    app.fire(search, 'input');
    app.runTimers();
    await flush();
    app.fire(app.panelEl('[data-ec-agents-add="c1"]'), 'click');
    app.fire(app.panelEl('[data-ec-agents-save]'), 'click');
    await flush();

    assert.equal(state.posts.length, 1);
    assert.equal(reads > 1, true, 'the relation is re-read after the save');
    assert.ok(app.root().innerHTML.indexOf(
        zh.ec_assign_summary_configured.replace('{n}', '3')) >= 0,
    'the card counts the relation the server just accepted');
});

test('a read-only caller sees the relation but every write control is unavailable', async () => {
    const state = { card: assignCard({ agent_assignment: { can_assign: false, visible_count: 2 } }) };
    const app = assignsPage(state, {
        read: () => envelope(assignPage([
            agentItem('a1', '阿尔法'), agentItem('a2', '贝塔', { manageable: false }),
        ], 2, 1, { can_assign: false })),
    });
    await app.page.load();
    await openAssignments(app);
    const panel = app.panel().innerHTML;
    assert.ok(panel.indexOf(zh.ec_assign_read_only) >= 0);
    // 可见但不可管理可以显示：the row is there, its control is not usable.
    assert.ok(panel.indexOf('贝塔') >= 0);
    assert.ok(panel.indexOf(zh.ec_assign_cannot_manage) >= 0);
    assert.match(panel, /disabled data-ec-agents-remove="a1"/,
        'even a manageable agent cannot be changed without the connection permission');
    assert.match(panel, /disabled data-ec-agents-save/);

    // A constructed change is refused too: the page does not even draft it.
    app.fire(app.panelEl('[data-ec-agents-remove="a1"]'), 'click');
    await drain();
    assert.deepEqual(drafted(app.page.assignmentState().remove), []);
    assert.equal(assignmentWrites(app).length, 0);
});

test('deleting a configured connection says its assignment relations go too', async () => {
    const state = { card: assignCard({ agent_assignment: { configured: true, visible_count: 2 } }) };
    const app = assignsPage(state, {});
    await app.page.load();
    app.page.confirmDelete(app.page.state.cards[0]);
    assert.ok(app.panel().innerHTML.indexOf(zh.ec_confirm_delete_assignments) >= 0,
        'the confirmation names the cleanup instead of leaving it to be discovered');

    // A connection outside the regime has nothing to clean up, so it is not
    // told that something will be.
    const platform = { card: assignCard({ scope: 'platform', actions: ['read', 'manage'] }) };
    delete platform.card.agent_assignment;
    const other = assignsPage(platform, {});
    await other.page.load();
    other.page.confirmDelete(other.page.state.cards[0]);
    assert.equal(other.panel().innerHTML.indexOf(zh.ec_confirm_delete_assignments), -1);
});

test('the assignment UI keeps the page\'s layout and i18n conventions', () => {    // Narrow screens: rows wrap and their controls become full width.
    assert.match(css, /@media \(max-width: 420px\)[\s\S]*\.ec-assign-pager, \.ec-assign-pager \.ec-btn \{ width: 100%/);
    assert.match(css, /\.ec-assign-list \{[\s\S]*min-width: 0/);
    assert.match(css, /\.dark \.ec-assign-name/);
    // Every string is a key, in all three languages (also asserted by the
    // namespace sweep above, restated here for the assignment block).
    ['zh', 'zh-Hant', 'en'].forEach(lang => {
        ['ec_action_agents', 'ec_assign_title', 'ec_assign_first_title',
         'ec_assign_conflict', 'ec_assign_unknown', 'ec_assign_save',
         'ec_assign_added', 'ec_toast_agents_saved'].forEach(key => {
            assert.ok(ns[lang][key] && String(ns[lang][key]).length > 0,
                `${lang} is missing ${key}`);
        });
    });
});

