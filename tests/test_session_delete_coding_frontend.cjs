// Deleting a coding session from history (change
// fix-coding-session-history-management).
//
// The shipped `deleteSession` handler is run against a small DOM and a
// controllable transport. The load-bearing promises are about *what the page
// does after the server confirms*:
//
//   * deleting the last current coding session closes the pane and shows an
//     empty state; it never starts a new remote session by itself;
//   * an adjacent session is resumed through the ordinary switch path;
//   * the unsaved-editor guard runs before the request, so a cancel sends
//     nothing;
//   * a failure or a late response cannot navigate, close or create anything;
//   * a stale composer cannot turn a send into an implicit coding creation.
//
// The backend half of the same change (a link read that must not be mistaken
// for "ordinary session") is tests/test_coding_session_routes.py.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');

function slice(from, to) {
    const start = source.indexOf(from);
    const end = source.indexOf(to, start);
    assert.ok(start >= 0 && end > start, `Missing source section: ${from}`);
    return source.slice(start, end);
}

// ------------------------------------------------------------------ DOM stub
function element(tagName = 'div') {
    const classes = new Set();
    let text = '', html = '';
    const el = {
        tagName: tagName.toUpperCase(), value: '', dataset: {}, attrs: {}, handlers: {},
        children: [], disabled: false, hidden: false, removed: false, parentNode: null,
        get firstChild() { return this.children[0] || null; },
        get textContent() { return text; },
        set textContent(value) { text = value; html = ''; },
        get innerHTML() { return html; },
        set innerHTML(value) { html = value; },
        classList: {
            add: (...names) => names.forEach(n => classes.add(n)),
            remove: (...names) => names.forEach(n => classes.delete(n)),
            contains: name => classes.has(name),
            toggle(name, force) {
                const add = force === undefined ? !classes.has(name) : force;
                if (add) classes.add(name); else classes.delete(name);
                return add;
            },
        },
        setAttribute(key, value) { this.attrs[key] = String(value); },
        removeAttribute(key) { delete this.attrs[key]; },
        hasAttribute(key) { return Object.prototype.hasOwnProperty.call(this.attrs, key); },
        getAttribute(key) { return key in this.attrs ? this.attrs[key] : null; },
        addEventListener(name, fn) { (this.handlers[name] = this.handlers[name] || []).push(fn); },
        click() { (this.handlers.click || []).forEach(fn => fn({ stopPropagation() {} })); },
        appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
        remove() { this.removed = true; },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
        querySelectorAll(selector) { return queryAll(el, selector); },
        focus() { this.focused = true; },
    };
    Object.defineProperty(el, 'className', {
        get: () => Array.from(classes).join(' '),
        set: value => {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach(c => classes.add(c));
        },
    });
    return el;
}

// A tiny matcher: a comma-separated list of `tag.class#id[data-x=y]` selectors.
function matches(el, selector) {
    let rest = selector.trim();
    const attrRegex = /\[([a-z-]+)="?([^\]"]*)"?\]/g;
    let m;
    const attrs = [];
    while ((m = attrRegex.exec(rest))) attrs.push([m[1], m[2]]);
    rest = rest.replace(attrRegex, '');
    const id = (rest.match(/#([\w-]+)/) || [])[1];
    const className = (rest.match(/\.([\w-]+)/) || [])[1];
    const tag = (rest.match(/^[a-zA-Z]+/) || [])[0];
    if (tag && el.tagName !== tag.toUpperCase()) return false;
    if (id && el.id !== id) return false;
    if (className && !el.classList.contains(className)) return false;
    return attrs.every(([key, value]) => {
        const prop = key.replace(/^data-/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        const actual = key.startsWith('data-') ? el.dataset[prop] : el.attrs[key];
        return value === '' ? actual != null : actual === value;
    });
}

function queryAll(root, selector) {
    const parts = selector.split(',').map(s => s.trim());
    const out = [];
    const walk = node => {
        node.children.forEach(child => {
            if (parts.some(part => matches(child, part))) out.push(child);
            walk(child);
        });
    };
    walk(root);
    return out;
}

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

async function settle() {
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
}

// ------------------------------------------------------------------ harness
function setup() {
    const nodes = new Map();
    const node = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
    const byId = id => nodes.get(id) || null;
    const messages = element();
    messages.id = 'chat-messages';
    nodes.set('chat-messages', messages);
    const composerArea = element();
    composerArea.id = 'chat-input-area';
    nodes.set('chat-input-area', composerArea);

    const requests = [];
    const toasts = [];
    const events = [];
    let respond = () => { throw new Error('no transport configured'); };

    const agents = [
        { id: 'erp-coder', name: 'Coding Agent', agent_type: 'coding' },
        { id: 'agent-x', name: 'Normal Agent', agent_type: 'normal' },
    ];
    const coding = {
        mounted: false, agentId: '', sessionId: '',
        leaves: [], launches: 0, opens: [],
        isActive() { return coding.mounted; },
        leave(options) { coding.leaves.push(options || {}); coding.mounted = false; return true; },
        current() {
            return coding.mounted
                ? { agent_id: coding.agentId, session_id: coding.sessionId } : null;
        },
        open(agentId, sessionId) {
            coding.opens.push([agentId, sessionId]);
            coding.mounted = true; coding.agentId = agentId; coding.sessionId = sessionId;
            return Promise.resolve({ session_id: sessionId, agent_id: agentId });
        },
        launch(agentId) {
            coding.launches += 1;
            coding.mounted = true; coding.agentId = agentId; coding.sessionId = 'created';
            return Promise.resolve({ session_id: 'created', agent_id: agentId });
        },
        refresh() { return Promise.resolve(true); },
        retry() { return Promise.resolve(true); },
        isCodingAgent() { return true; },
    };

    const ctx = vm.createContext({
        console, Date, Intl, URL, Request, Headers, URLSearchParams, AbortController,
        Promise, JSON, Object, Array, String, Number, Boolean, Error, Math,
        encodeURIComponent, decodeURIComponent, setImmediate,
        window: {
            innerWidth: 1440,
            location: { href: 'http://test/chat', origin: 'http://test' },
            CodingChat: coding,
            addEventListener() {},
        },
        document: {
            getElementById: id => byId(id),
            createElement: element,
            querySelector: selector => queryAll(messages, selector)[0] || null,
            querySelectorAll: selector => queryAll(messages, selector),
            addEventListener() {},
        },
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        queueMicrotask() {},
        requestAnimationFrame: fn => fn(),
        setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: id => clearTimeout(id),
        activeAgentId: 'erp-coder', defaultAgentId: 'agent-x', sessionId: 'coding-1',
        currentView: 'history', _authEpoch: 1, _historyVisible: true, _historyDirty: false,
        _sessCfg: null, _wsSelState: { current: null },
        _autoScrollEnabled: true, _lastScrollTop: 0, _programmaticScrollTop: null,
        _historyPage: 0, _historyHasMore: false, _historyLoadSeq: 0, _historyLoadContext: '',
        messagesDiv: messages,
        t: key => key, escapeHtml: value => String(value),
        _wsToast: message => toasts.push(message),
        paintCodingService() {},
        _isOptimisticSession: () => false,
        _sessionListContext: () => 'ctx',
        _invalidateHistoryCache() {},
        _refreshHistoryList() { events.push('refresh'); },
        writeScopedPreference() {}, removeScopedPreference() {},
        activeSessionStorageKey: () => 'active-session',
        findAgent: id => agents.find(a => a.id === id) || null,
        availableChatAgents: () => agents,
        resetTimeline() { events.push('reset-timeline'); },
        renderComposerIdentity() { events.push('render-identity'); },
        renderWelcomeScreen() { events.push('welcome'); },
        syncChatHomeLayout() {},
        openSessionPanel() {}, finishSessionPanelSelection() {},
        _addOptimisticSessionItem() {}, _contextAfterSessionChange() {},
        _wsSelUpdateLabel() {}, refreshWorkspaceSelector() {},
        refreshSessionSettings: async () => {}, wsOnSessionSwitch() {},
        resetSendBtnSendMode() {}, startPolling() {}, _desktopContextClear() {},
        navigateTo(view) { ctx.currentView = view; },
        // A clean editor: return true without invoking the callback, which is
        // exactly what workspace.js's guard does when nothing is unsaved.
        wsGuardUnsaved: () => true,
        generateSessionId: () => 'generated-session',
        _wsAttr: value => String(value),
    });
    ctx.fetch = (url, options) => {
        const record = { url, options: options || {} };
        requests.push(record);
        return respond(record);
    };
    const run = code => vm.runInContext(code, ctx);

    run(slice('function codingChatModule()', 'function switchSession('));
    run(slice('function newChat(', '// Session History (workbench page)'));
    // The shared row-action in-flight set and its busy marker, then
    // deleteSession + _findNextSession + the coding empty state helpers.
    run(slice('// One row action is submitted at most once until its answer lands',
        'function _isOptimisticSession('));
    run(slice('function deleteSession(', 'function showConfirmModal('));
    // The seam is read through codingSeam(); switchSession is stubbed so the
    // test observes the delete handler's choice, not the switch internals.
    ctx.switchSession = (sid, agent) => events.push(['switch', sid, agent]);
    ctx.showConfirmModal = (title, message, onConfirm) => onConfirm();

    const row = (sid, agent, className = 'session-item') => {
        const el = element('div');
        el.className = className;
        el.dataset.sessionId = sid;
        el.dataset.agentId = agent;
        messages.appendChild(el);
        return el;
    };
    return { ctx, run, requests, toasts, events, coding, row, composerArea, messages };
}

const okDelete = () => Promise.resolve({
    ok: true, status: 200, json: async () => ({ status: 'success' }),
});

// ==================================================== delete handler
test('deleting the last current coding session closes the pane into an empty state without creating', async () => {
    const h = setup();
    h.row('coding-1', 'erp-coder');
    h.coding.mounted = true; h.coding.agentId = 'erp-coder'; h.coding.sessionId = 'coding-1';
    h.ctx.fetch = (url, options) => {
        h.requests.push({ url, options: options || {} });
        if ((options && options.method) === 'DELETE') return okDelete();
        return Promise.resolve({ ok: true, json: async () => ({ status: 'success' }) });
    };

    h.ctx.deleteSession('coding-1', 'erp-coder');
    await settle();

    const deletes = h.requests.filter(r => r.options.method === 'DELETE');
    assert.equal(deletes.length, 1);
    assert.equal(deletes[0].url, '/api/sessions/coding-1?agent_id=erp-coder');

    // The pane is torn down without asking again (the guard ran pre-request).
    assert.equal(h.coding.leaves.length, 1);
    assert.equal(h.coding.leaves[0].skipConfirm, true);
    // No implicit remote session: launch/open never ran.
    assert.equal(h.coding.launches, 0);
    assert.equal(h.coding.opens.length, 0);
    // The empty state is on screen, the Agent stayed selected, and the
    // ordinary composer is withheld.
    assert.ok(h.messages.querySelector('#coding-empty-state'), 'empty state is rendered');
    assert.equal(h.ctx.activeAgentId, 'erp-coder');
    assert.equal(h.ctx.sessionId, '');
    assert.equal(h.composerArea.classList.contains('hidden'), true);
});

test('deleting the current coding session with an adjacent session switches to it', async () => {
    const h = setup();
    h.row('coding-1', 'erp-coder');
    h.row('normal-2', 'agent-x');
    h.coding.mounted = true; h.coding.agentId = 'erp-coder'; h.coding.sessionId = 'coding-1';
    h.ctx.fetch = (url, options) => {
        h.requests.push({ url, options: options || {} });
        return okDelete();
    };

    h.ctx.deleteSession('coding-1', 'erp-coder');
    await settle();

    assert.deepEqual(h.events.find(e => Array.isArray(e) && e[0] === 'switch'), ['switch', 'normal-2', 'agent-x']);
    assert.equal(h.messages.querySelector('#coding-empty-state'), null);
});

test('a canceled unsaved guard never submits the delete', async () => {
    const h = setup();
    h.row('coding-1', 'erp-coder');
    h.coding.mounted = true; h.coding.agentId = 'erp-coder'; h.coding.sessionId = 'coding-1';
    let asked = 0;
    h.ctx.wsGuardUnsaved = proceed => { asked += 1; return false; };

    h.ctx.deleteSession('coding-1', 'erp-coder');
    await settle();

    assert.equal(asked, 1, 'the guard was consulted before the request');
    assert.equal(h.requests.filter(r => r.options.method === 'DELETE').length, 0);
    assert.equal(h.coding.leaves.length, 0);
    assert.equal(h.ctx.sessionId, 'coding-1');
});

test('a failed delete keeps the session and reports the reason', async () => {
    const h = setup();
    h.row('coding-1', 'erp-coder');
    h.coding.mounted = true; h.coding.agentId = 'erp-coder'; h.coding.sessionId = 'coding-1';
    h.ctx.fetch = (url, options) => {
        h.requests.push({ url, options: options || {} });
        return Promise.resolve({ ok: true, status: 200, json: async () => ({ status: 'error', message: '关联异常' }) });
    };

    h.ctx.deleteSession('coding-1', 'erp-coder');
    await settle();

    assert.deepEqual(h.toasts, ['关联异常']);
    assert.equal(h.coding.leaves.length, 0);
    assert.equal(h.ctx.sessionId, 'coding-1');
    assert.equal(h.messages.querySelector('#coding-empty-state'), null);
});

test('a late delete response cannot close a session opened meanwhile', async () => {
    const h = setup();
    h.row('coding-1', 'erp-coder');
    h.coding.mounted = true; h.coding.agentId = 'erp-coder'; h.coding.sessionId = 'coding-1';
    const pending = deferred();
    h.ctx.fetch = (url, options) => {
        h.requests.push({ url, options: options || {} });
        return pending.promise;
    };

    h.ctx.deleteSession('coding-1', 'erp-coder');
    await settle();
    // The user opens another conversation before the answer lands.
    h.ctx.sessionId = 'other-session';
    h.ctx.activeAgentId = 'agent-x';
    pending.resolve(await okDelete());
    await settle();

    assert.equal(h.coding.leaves.length, 0, 'the new session must not be closed');
    assert.equal(h.messages.querySelector('#coding-empty-state'), null);
    assert.equal(h.events.some(e => Array.isArray(e) && e[0] === 'switch'), false);
    assert.equal(h.ctx.sessionId, 'other-session');
});

test('a plain session with no neighbour still starts an unpersisted new chat', async () => {
    const h = setup();
    h.row('plain-1', 'agent-x');
    h.ctx.activeAgentId = 'agent-x';
    h.ctx.sessionId = 'plain-1';
    h.ctx.fetch = (url, options) => {
        h.requests.push({ url, options: options || {} });
        return okDelete();
    };

    h.ctx.deleteSession('plain-1', 'agent-x');
    await settle();

    assert.equal(h.coding.launches, 0);
    assert.equal(h.messages.querySelector('#coding-empty-state'), null);
    assert.equal(h.ctx.sessionId, 'generated-session', 'the ordinary unpersisted new chat still happens');
});

test('a row under an unanswered delete is marked busy, deduped, then released', async () => {
    const h = setup();
    const row = h.row('coding-1', 'erp-coder');
    h.coding.mounted = true; h.coding.agentId = 'erp-coder'; h.coding.sessionId = 'coding-1';
    const pending = deferred();
    h.ctx.fetch = (url, options) => {
        h.requests.push({ url, options: options || {} });
        return pending.promise;
    };

    h.ctx.deleteSession('coding-1', 'erp-coder');
    await settle();
    const deletes = () => h.requests.filter(r => r.options.method === 'DELETE');
    assert.equal(row.classList.contains('busy'), true, 'the row shows its own request is in flight');
    assert.equal(deletes().length, 1);

    // A second click while the first is unanswered is dropped: a confirm
    // dialog cannot produce two deletes of the same session.
    h.ctx.deleteSession('coding-1', 'erp-coder');
    await settle();
    assert.equal(deletes().length, 1);

    pending.resolve(await okDelete());
    await settle();
    assert.equal(row.classList.contains('busy'), false, 'the row is released when the answer lands');
});

// ==================================================== send guard
test('a coding agent message with no mounted pane never creates a session', async () => {
    const h = setup();
    h.ctx.activeAgentId = 'erp-coder';
    h.ctx.sessionId = '';

    assert.equal(h.ctx.guardCodingMessage(), true);
    await settle();

    assert.deepEqual(h.toasts, ['coding_send_in_pane']);
    assert.equal(h.coding.launches, 0);
    assert.equal(h.coding.opens.length, 0);
});
