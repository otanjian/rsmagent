// Coding sessions in the platform chat pane (change add-opencode-coding-agents,
// tasks 4.3 / 4.4 / 4.5).
//
// The load-bearing promises here are about *when* the platform acts, so they are
// asserted against the shipped `static/js/coding.js` (loaded whole into a vm)
// with controlled timers and transport, not restated as a description:
//
//   * an embedded coding session replaces the ordinary composer and leaving
//     restores exactly what was there -- including a control that was already
//     hidden for its own reasons;
//   * a notification is trusted only when origin, window, channel and shape all
//     agree, so a stale iframe cannot register a session and a duplicate
//     delivery cannot register twice;
//   * the 5s refresh stops while the page is hidden, performs exactly one
//     catch-up round when it returns, collapses concurrent rounds into one, and
//     drops a late response once the pane has moved on;
//   * a failed round keeps the cached rows (it never removes anything), and the
//     next good round redraws once.
//
// The page integration is checked against the real ChatHandler assembly in
// tests/test_web_console_assets.py; this file checks the markup the page loads.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const read = p => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const codingJs = read('channel/web/static/js/coding.js');
const codingCss = read('channel/web/static/css/coding.css');
const i18nJs = read('channel/web/static/js/i18n/coding.js');
const chatHtml = read('channel/web/chat.html');
const consoleJs = read('channel/web/static/js/console.js');

// The real strings, so a message the user sees is asserted as written rather
// than as an i18n key the test invented.
const NS = (() => {
    const ctx = { window: {} };
    vm.createContext(ctx);
    vm.runInContext(i18nJs, ctx, { filename: 'coding-i18n.js' });
    return ctx.window.__cowI18N__['coding'];
})();

// ---------------------------------------------------------------- DOM stub
function makeElement(tag) {
    const attrs = {};
    const classes = new Set();
    const children = [];
    const handlers = {};
    const element = {
        tagName: String(tag).toUpperCase(),
        children, handlers, attrs,
        textContent: '', src: '', type: '', title: '', parent: null, removed: false,
        classList: {
            add: (...xs) => xs.forEach(x => classes.add(x)),
            remove: (...xs) => xs.forEach(x => classes.delete(x)),
            contains: x => classes.has(x),
            toggle: (x, on) => {
                const next = on === undefined ? !classes.has(x) : !!on;
                if (next) classes.add(x); else classes.delete(x);
                return next;
            },
        },
        setAttribute: (k, v) => { attrs[k] = String(v); },
        getAttribute: k => (k in attrs ? attrs[k] : null),
        hasAttribute: k => k in attrs,
        removeAttribute: k => { delete attrs[k]; },
        appendChild: child => { children.push(child); child.parent = element; return child; },
        remove: () => {
            element.removed = true;
            if (!element.parent) return;
            const index = element.parent.children.indexOf(element);
            if (index >= 0) element.parent.children.splice(index, 1);
            element.parent = null;
        },
        addEventListener: (type, fn) => { (handlers[type] = handlers[type] || []).push(fn); },
        click: () => (handlers.click || []).forEach(fn => fn()),
    };
    Object.defineProperty(element, 'className', {
        get: () => Array.from(classes).join(' '),
        set: value => {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach(c => classes.add(c));
        },
    });
    return element;
}

// The page's own elements the module reads by id, each with the classes the
// real markup carries -- a control that is already hidden must stay hidden
// after the pane is restored.
const CHROME = [
    { id: 'chat-main', className: 'chat-main chat-home relative' },
    { id: 'chat-messages', className: 'flex-1 overflow-y-auto' },
    { id: 'chat-input-area', className: 'flex-shrink-0 px-4 py-3' },
    { id: 'scroll-to-bottom-btn', className: 'hidden absolute right-5' },
];
// A control the platform hides for its own reason (an unavailable action):
// leaving coding mode must not reveal it.
const ALREADY_HIDDEN = { id: 'session-context-host', className: 'hidden' };

// ------------------------------------------------------------ fake timers
function makeClock() {
    let nextId = 1;
    let now = 0;
    const timers = new Map();
    return {
        now: () => now,
        set: (fn, ms) => { const id = nextId++; timers.set(id, { fn, at: now + (ms || 0) }); return id; },
        clear: id => { timers.delete(id); },
        pending: () => timers.size,
        // Advance the virtual clock, firing due timers in time order. A tick
        // that reschedules itself (the refresh loop) is therefore fired the
        // right number of times.
        advance: ms => {
            const target = now + ms;
            for (let guard = 0; guard < 10000; guard += 1) {
                let dueId = null;
                let dueAt = Infinity;
                timers.forEach((timer, id) => {
                    if (timer.at <= target && timer.at < dueAt) { dueAt = timer.at; dueId = id; }
                });
                if (dueId === null) break;
                const timer = timers.get(dueId);
                timers.delete(dueId);
                now = timer.at;
                timer.fn();
            }
            now = target;
        },
    };
}

// -------------------------------------------------------------- harness
function mount() {
    const clock = makeClock();
    const byId = new Map();
    const created = [];
    const document = {
        createElement: tag => { const el = makeElement(tag); created.push(el); return el; },
        getElementById: id => byId.get(id) || null,
        querySelectorAll: selector => {
            const parts = selector.split(',').map(s => s.trim());
            return created.filter(el => !el.removed && parts.some(part => {
                if (part.startsWith('#')) return el.getAttribute('id') === part.slice(1);
                if (part === '[data-coding-hide]') return el.hasAttribute('data-coding-hide');
                return false;
            }));
        },
    };
    CHROME.forEach(spec => {
        const el = makeElement('div');
        el.setAttribute('id', spec.id);
        el.className = spec.className;
        byId.set(spec.id, el);
        created.push(el);
    });
    {
        const el = makeElement('div');
        el.setAttribute('id', ALREADY_HIDDEN.id);
        el.setAttribute('data-coding-hide', '');
        el.className = ALREADY_HIDDEN.className;
        byId.set(ALREADY_HIDDEN.id, el);
        created.push(el);
    }

    const calls = { requests: [], notes: [], redraws: 0, leaves: 0, linked: [], confirms: 0 };
    const window = {
        location: { origin: 'https://console.example.com', href: 'https://console.example.com/chat' },
        setTimeout: clock.set,
        clearTimeout: clock.clear,
        // The zh copy is the deployment default here, so an assertion on a
        // message is an assertion on what the user reads.
        t: key => NS.zh[key] || key,
        showNotification: message => calls.notes.push(message),
    };
    const ctx = vm.createContext({
        window, document, console,
        URL, Date, Math, JSON, Promise, String, Number, Boolean, Array, Object, Error,
        encodeURIComponent, decodeURIComponent,
    });
    vm.runInContext(codingJs, ctx, { filename: 'coding.js' });
    const CodingChat = window.CodingChat;

    // The transport records every request and answers from a per-path queue, so
    // a test decides what the service says and in what order it resolves.
    const routes = new Map();
    let pending = [];
    const respond = (path, handler) => routes.set(path, handler);
    const queue = (path, responses) => {
        const remaining = responses.slice();
        routes.set(path, () => (remaining.length > 1 ? remaining.shift() : remaining[0]));
    };
    const live = el => !el.removed;
    const find = cls => created.find(el => live(el) && el.className === cls) || null;

    async function flush(rounds) {
        for (let i = 0; i < (rounds || 16); i += 1) await Promise.resolve();
    }
    /** Resolve every request issued so far, then let the chains run. */
    async function settle(result) {
        await flush();
        const batch = pending.splice(0, pending.length);
        batch.forEach(({ record, handler }) => record.resolve(result || handler()));
        await flush();
        return batch.length;
    }
    /** Settle until the module stops asking (a cursor walk, a retry chain). */
    async function pump(limit) {
        let rounds = 0;
        while (rounds < (limit || 20)) {
            const count = await settle();
            if (!count) break;
            rounds += 1;
        }
        await flush();
        return rounds;
    }
    /** Start an action, settle it, and hand back its result. */
    async function run(start) {
        const promise = start();
        await pump();
        return promise;
    }
    function reset() {
        pending = [];
        calls.requests.length = 0;
        calls.notes.length = 0;
        calls.redraws = 0;
    }

    CodingChat.setHooks({
        request: (path, options) => new Promise(resolve => {
            const record = { path, options, resolve: result => {
                calls.requests.push({ path: record.path, options: record.options });
                resolve(result);
            } };
            const handler = routes.get(path.split('?')[0]);
            pending.push({ record, handler: handler || (() => ({ ok: true, status: 200, data: {} })) });
        }),
        notify: message => calls.notes.push(message),
        redrawList: () => { calls.redraws += 1; },
        confirmLeave: proceed => { calls.confirms += 1; proceed(); return true; },
        onLeave: () => { calls.leaves += 1; },
        onLinked: data => calls.linked.push(data),
        now: clock.now,
        setTimer: clock.set,
        clearTimer: clock.clear,
        randomId: (() => { let n = 0; return () => `req-${++n}`; })(),
    });

    return {
        CodingChat, calls, clock, window, byId, reset, run, pump, settle, flush, respond, queue,
        count: p => calls.requests.filter(r => r.path.split('?')[0] === p).length,
        pendingCount: () => pending.length,
        frame: () => find('coding-frame'),
        loading: () => find('coding-loading'),
        retryBox: () => find('coding-retry'),
        hidden: id => byId.get(id).classList.contains('hidden'),
        isHidden: id => byId.get(id).classList.contains('hidden'),
    };
}

const PAYLOAD = {
    session_id: 'sess-1',
    agent_id: 'erp-coder',
    external_session_id: 'ses_remote_1',
    state: 'ready',
    project_dir: '/tmp/rsm/project',
    iframe_url: 'https://code.example.com/L3RtcC9wcm9qZWN0/session/ses_remote_1',
};

const SYNC_OK = { ok: true, status: 200, data: { changed: [], removed: [], unavailable: [], next_cursor: null } };

/** Open a coding session the ordinary way, as a click in the list would. */
async function openSession(h, sessionId = 'sess-1') {
    const payload = sessionId === 'sess-1'
        ? PAYLOAD
        : Object.assign({}, PAYLOAD, { session_id: sessionId });
    h.respond(`/api/coding/sessions/${sessionId}/open`, () => ({ ok: true, status: 200, data: payload }));
    const opened = await h.run(() => h.CodingChat.open('erp-coder', sessionId));
    assert.ok(opened && opened.session_id, 'the session opened');
    return opened;
}

/** The channel the mounted frame was given, plus a usable `source` for notes.
 *
 *  ``contentWindow`` is what the module compares against, so the fake frame
 *  carries one and the notification tests drive the real check rather than a
 *  stand-in for it. */
function frameIdentity(h) {
    const frame = h.frame();
    frame.contentWindow = { id: 'the-frame' };
    return { channel: h.CodingChat._state().channel, source: frame.contentWindow, origin: 'https://code.example.com' };
}

// ================================================================ assets
test('the loaded page references the coding module, its styles and its strings', () => {
    // ChatHandler assembles the page from chat.html, so a module the shell does
    // not reference is a module the browser never runs. Referencing it as a
    // first-party asset is also what gets it stamped: the assembler versions
    // every `js/**` and `css/**` URL it finds (channel/web/core/template.py),
    // which is why the handler no longer carries a list of names to stamp.
    assert.match(chatHtml, /assets\/js\/coding\.js/);
    assert.match(chatHtml, /assets\/css\/coding\.css/);
    assert.match(chatHtml, /assets\/js\/i18n\/coding\.js/);
    assert.ok(codingCss.length > 0);
    // The served half of the same guarantee -- the reference really comes back
    // stamped -- is tests/test_coding_page_assets.py, which drives the handler.
});

test('coding.js is evaluated before console.js consumes it', () => {
    // console.js wires window.CodingChat during its own init, so a later script
    // tag would leave it undefined for the whole page.
    const order = [chatHtml.indexOf('assets/js/coding.js'), chatHtml.indexOf('assets/js/console.js')];
    assert.ok(order[0] >= 0 && order[1] >= 0, 'both scripts referenced');
    assert.ok(order[0] < order[1], 'coding.js precedes console.js');
});

test('the module exposes exactly one namespace', () => {
    const h = mount();
    assert.equal(h.CodingChat.source, 'channel/web/static/js/coding.js');
    for (const name of ['launch', 'open', 'refresh', 'leave']) {
        assert.equal(typeof h.CodingChat[name], 'function', name);
    }
});

// ===================================================== switching by type
test('opening a coding session mounts the frame and hides the ordinary composer', async () => {
    const h = mount();
    await openSession(h);

    const frame = h.frame();
    assert.ok(frame, 'a frame is mounted');
    // The frame is the embed: the page supplies the parent origin and a fresh
    // channel of its own, never the server.
    assert.match(frame.src, /[?&]rsm_embed=1/);
    assert.match(frame.src, /rsm_parent_origin=https%3A%2F%2Fconsole\.example\.com/);
    assert.match(frame.src, new RegExp('rsm_channel=' + h.CodingChat._state().channel));

    assert.equal(h.isHidden('chat-messages'), true);
    assert.equal(h.isHidden('chat-input-area'), true);
    assert.equal(h.isHidden('chat-main'), false);
    assert.equal(h.byId.get('chat-main').classList.contains('coding-active'), true);
    assert.equal(h.CodingChat.isActive(), true);
});

test('leaving restores the composer and keeps a control that was already hidden hidden', async () => {
    const h = mount();
    await openSession(h);
    assert.equal(h.CodingChat.leave(), true);

    assert.equal(h.isHidden('chat-messages'), false);
    assert.equal(h.isHidden('chat-input-area'), false);
    assert.equal(h.byId.get('chat-main').classList.contains('coding-active'), false);
    // It was hidden before coding was entered, and it is still hidden: the
    // restore only undoes what this module did.
    assert.equal(h.isHidden(ALREADY_HIDDEN.id), true);
    assert.equal(h.frame(), null, 'the frame is gone');
    assert.equal(h.CodingChat.isActive(), false);
    assert.equal(h.calls.leaves, 1);
});

test('the empty-chat layout is dropped while the pane owns the area, and restored on leaving', async () => {
    const h = mount();
    const main = h.byId.get('chat-main');
    assert.equal(main.classList.contains('chat-home'), true, 'the fixture starts on the home view');

    await openSession(h);

    // ``chat-home`` describes an empty chat: it centres a hero, pads the pane's
    // top and makes the message list ``display: contents``. A mounted pane owns
    // the whole area, and the welcome markup is still in the DOM (a coding
    // conversation has no platform messages to remove it), so leaving the class
    // on shares the column between the frame and that markup and collapses the
    // frame to a strip -- which is exactly what a reopen from history did.
    assert.equal(main.classList.contains('chat-home'), false);

    assert.equal(h.CodingChat.leave(), true);
    assert.equal(main.classList.contains('chat-home'), true, 'the home view comes back');
});

test('a pane mounted off the home view does not turn the home view on when it leaves', async () => {
    const h = mount();
    const main = h.byId.get('chat-main');
    main.classList.remove('chat-home');

    await openSession(h);
    assert.equal(main.classList.contains('chat-home'), false);

    assert.equal(h.CodingChat.leave(), true);
    // Same rule as an already-hidden control: the restore only undoes what this
    // module did, so a conversation that was open stays open.
    assert.equal(main.classList.contains('chat-home'), false);
});


test('the frame URL carries exactly one embed marker, whoever supplied it', async () => {
    // The server builds the session URL and already marks it (`session_url` in
    // agent/coding/sessions.py owns ``rsm_embed``); the page adds it only for a
    // URL that arrives without one, so a mount never hands the child a
    // duplicated parameter.
    const marked = Object.assign({}, PAYLOAD, {
        session_id: 'sess-marked',
        iframe_url: PAYLOAD.iframe_url + '?rsm_embed=1',
    });
    const h = mount();
    h.respond('/api/coding/sessions/sess-marked/open',
        () => ({ ok: true, status: 200, data: marked }));
    await h.run(() => h.CodingChat.open('erp-coder', 'sess-marked'));

    const duplicated = h.frame().src;
    assert.equal(duplicated.split('rsm_embed=1').length - 1, 1, duplicated);
    assert.match(duplicated, /rsm_parent_origin=/);
    assert.match(duplicated, /rsm_channel=/);

    // A URL the platform did not mark still gets one: the child decides by it.
    const h2 = mount();
    await openSession(h2);
    assert.equal(h2.frame().src.split('rsm_embed=1').length - 1, 1, h2.frame().src);
});

test('opening another coding session replaces the frame instead of stacking one', async () => {
    const h = mount();
    await openSession(h, 'sess-1');
    const first = h.frame();
    await openSession(h, 'sess-2');

    assert.notEqual(h.frame(), first);
    assert.equal(first.removed, true, 'the previous frame was removed');
    assert.equal(h.CodingChat._state().sessionId, 'sess-2');
});

test('switching away obeys the console leave confirmation', async () => {
    const h = mount();
    await openSession(h);

    h.CodingChat.setHooks({ confirmLeave: () => false });
    assert.equal(h.CodingChat.leave(), false, 'a refused leave changes nothing');
    assert.equal(h.CodingChat.isActive(), true);
    assert.equal(h.isHidden('chat-input-area'), true);

    let allowed = false;
    h.CodingChat.setHooks({ confirmLeave: proceed => { allowed = true; proceed(); return true; } });
    assert.equal(h.CodingChat.leave(), true);
    assert.equal(allowed, true);
});

// ======================================================== refresh cadence
test('the pane refreshes every five seconds and hidden pages stop asking', async () => {
    const h = mount();
    h.respond('/api/coding/sessions/sync', () => SYNC_OK);
    await openSession(h);
    assert.equal(h.CodingChat.constants.REFRESH_INTERVAL_MS, 5000);
    assert.equal(h.count('/api/coding/sessions/sync'), 0, 'mounting alone does not poll');

    h.clock.advance(5000);
    await h.settle();
    assert.equal(h.count('/api/coding/sessions/sync'), 1, 'one round after one interval');

    // Hidden: the timer keeps ticking but does no work.
    h.CodingChat.setVisibility(false);
    h.clock.advance(20000);
    await h.flush();
    assert.equal(h.pendingCount(), 0, 'a hidden page issues no requests');
});

test('returning to a visible page performs exactly one catch-up round', async () => {
    const h = mount();
    h.respond('/api/coding/sessions/sync', () => SYNC_OK);
    await openSession(h);
    h.CodingChat.setVisibility(false);
    h.clock.advance(15000);
    h.reset();

    h.CodingChat.setVisibility(true);
    await h.settle();
    assert.equal(h.count('/api/coding/sessions/sync'), 1, 'the catch-up is immediate and single');
});

test('concurrent refreshes collapse into the one in flight', async () => {
    const h = mount();
    h.respond('/api/coding/sessions/sync', () => SYNC_OK);
    await openSession(h);

    // Repeated clicks (and the interval firing during a slow round) must not
    // multiply the requests.
    const first = h.CodingChat.refresh();
    const second = h.CodingChat.refresh();
    const third = h.CodingChat.refresh();
    await h.flush();
    assert.equal(h.pendingCount(), 1, 'one request in flight');
    await h.settle();
    assert.equal(await first, true);
    assert.equal(await second, true);
    assert.equal(await third, true);
    assert.equal(h.count('/api/coding/sessions/sync'), 1);
});

test('a page is walked by its stable cursor across batches', async () => {
    const h = mount();
    h.queue('/api/coding/sessions/sync', [
        { ok: true, status: 200, data: { changed: [], next_cursor: 'cursor-2' } },
        { ok: true, status: 200, data: { changed: [], next_cursor: null } },
    ]);
    await openSession(h);
    h.reset();

    assert.equal(await h.run(() => h.CodingChat.refresh()), true);
    const batches = h.calls.requests.filter(r => r.path.split('?')[0] === '/api/coding/sessions/sync');
    assert.equal(batches.length, 2, 'the walk continues with the returned cursor');
    // Field-by-field: the body was built inside the module's own realm, so a
    // structural comparison would be comparing two different Object prototypes.
    assert.equal(batches[0].options.body.agent_id, 'erp-coder');
    assert.equal(batches[0].options.body.cursor, null);
    assert.equal(batches[1].options.body.agent_id, 'erp-coder');
    assert.equal(batches[1].options.body.cursor, 'cursor-2');
    assert.equal(h.calls.redraws, 1, 'the list is redrawn once, after the walk');
});

test('a late round is dropped once the pane has moved on', async () => {
    const h = mount();
    await openSession(h);

    const round = h.CodingChat.refresh();
    await h.flush();
    // The user leaves while the response is in the air. Its result must not
    // touch the pane or redraw the list.
    assert.equal(h.CodingChat.leave(), true);
    await h.settle({ ok: true, status: 200, data: { changed: [{ session_id: 'sess-1' }], next_cursor: null } });
    assert.equal(await round, false, 'the stale round reports it was discarded');
    assert.equal(h.calls.redraws, 0);
});

test('a failed round keeps the cached list and the next good round redraws once', async () => {
    const h = mount();
    h.queue('/api/coding/sessions/sync', [
        { ok: false, status: 502, data: { message: '编码服务暂时不可用' } },
        SYNC_OK,
    ]);
    await openSession(h);
    h.reset();

    // 502 is "the service is unreachable", not "the rows are gone": nothing is
    // removed and the user is not shown an error for a transient condition.
    assert.equal(await h.run(() => h.CodingChat.refresh()), false);
    assert.equal(h.calls.redraws, 0, 'a failed round draws nothing');
    assert.deepEqual(h.calls.notes, [], 'and asks nothing of the user');

    assert.equal(await h.run(() => h.CodingChat.refresh()), true);
    assert.equal(h.calls.redraws, 1);
});

test('rows the service cannot reach are left alone rather than dropped', async () => {
    const h = mount();
    h.respond('/api/coding/sessions/sync', () => ({
        ok: true, status: 200,
        data: { changed: [], removed: [], unavailable: ['sess-1'], next_cursor: null },
    }));
    await openSession(h);
    h.reset();

    // The server reports the row as unavailable, which is its way of saying
    // "keep showing the cached row"; the client must not translate that into a
    // removal or an error message.
    assert.equal(await h.run(() => h.CodingChat.refresh()), true);
    assert.equal(h.CodingChat.isActive(), true);
    assert.deepEqual(h.calls.notes, []);
});

// ============================================================ notifications
test('a ready notice from the mounted frame clears the waiting state', async () => {
    const h = mount();
    await openSession(h);
    assert.ok(h.loading(), 'the pane says it is working while it waits');

    const frame = frameIdentity(h);
    assert.equal(h.CodingChat.handleMessage({
        origin: frame.origin,
        source: frame.source,
        data: { type: 'rsm.opencode.ready', channel: frame.channel },
    }), true);
    assert.equal(h.CodingChat._state().ready, true);
    assert.equal(h.loading(), null);
});

test('the frame window itself is required, not merely its origin', async () => {
    const h = mount();
    await openSession(h);
    const frame = frameIdentity(h);
    const ready = { type: 'rsm.opencode.ready', channel: frame.channel };

    // Same origin, different window (anything else served from that host).
    assert.equal(h.CodingChat.handleMessage({
        origin: frame.origin, source: { id: 'another-window' }, data: ready,
    }), false);
    // Right window, wrong origin (a redirect or a foreign embed).
    assert.equal(h.CodingChat.handleMessage({
        origin: 'https://evil.example.com', source: frame.source, data: ready,
    }), false);
    // Right window and origin, channel from another mount.
    assert.equal(h.CodingChat.handleMessage({
        origin: frame.origin, source: frame.source,
        data: { type: 'rsm.opencode.ready', channel: 'ch-someone-else' },
    }), false);
    // Unknown shape.
    assert.equal(h.CodingChat.handleMessage({
        origin: frame.origin, source: frame.source,
        data: { type: 'rsm.other.event', channel: frame.channel },
    }), false);
    assert.equal(h.CodingChat._state().ready, false, 'nothing was accepted');
});

test('a session notice registers the session once and never reloads the frame', async () => {
    const h = mount();
    h.respond('/api/coding/sessions/attach', () => ({
        ok: true, status: 200,
        data: { session_id: 'sess-fork', external_session_id: 'ses_fork', agent_id: 'erp-coder' },
    }));
    await openSession(h);
    const frame = frameIdentity(h);
    const before = h.frame();

    const note = {
        origin: frame.origin,
        source: frame.source,
        data: { type: 'rsm.opencode.session', channel: frame.channel, session_id: 'ses_fork' },
    };
    assert.equal(h.CodingChat.handleMessage(note), true);
    await h.flush();
    // The duplicate delivery an iframe can produce (a re-render, a re-fire of
    // the same navigation) must not register twice.
    assert.equal(h.CodingChat.handleMessage(note), true);
    await h.pump();

    const attaches = h.calls.requests.filter(r => r.path.split('?')[0] === '/api/coding/sessions/attach');
    assert.equal(attaches.length, 1, 'one attach for one session');
    assert.equal(attaches[0].options.body.agent_id, 'erp-coder');
    assert.equal(attaches[0].options.body.source_session_id, 'sess-1');
    assert.equal(attaches[0].options.body.external_session_id, 'ses_fork');

    // Registration updates the selection; the frame already shows the session,
    // so it is not reloaded (that is what would loop).
    assert.equal(h.frame(), before);
    assert.equal(h.calls.linked.length, 1);
    assert.equal(h.calls.redraws, 1, 'the history list is refreshed for the new row');
});

test('a notice naming the session already shown is ignored', async () => {
    const h = mount();
    h.respond('/api/coding/sessions/attach', () => ({ ok: true, status: 200, data: {} }));
    await openSession(h);
    const frame = frameIdentity(h);

    assert.equal(h.CodingChat.handleMessage({
        origin: frame.origin,
        source: frame.source,
        data: {
            type: 'rsm.opencode.session',
            channel: frame.channel,
            session_id: PAYLOAD.external_session_id,
        },
    }), true);
    await h.pump();
    assert.equal(h.count('/api/coding/sessions/attach'), 0);
});

test('a failed attach says so and can be retried', async () => {
    const h = mount();
    h.respond('/api/coding/sessions/attach', () => ({
        ok: false, status: 403, data: { code: 'coding_project_mismatch', message: '项目目录与该项目不一致' },
    }));
    await openSession(h);
    const frame = frameIdentity(h);
    const note = {
        origin: frame.origin,
        source: frame.source,
        data: { type: 'rsm.opencode.session', channel: frame.channel, session_id: 'ses_fork' },
    };

    h.CodingChat.handleMessage(note);
    await h.pump();
    // The reason comes from the server, not from a client-side guess.
    assert.ok(h.calls.notes.some(m => String(m).includes('项目目录与该项目不一致')), h.calls.notes.join(' | '));
    assert.equal(h.calls.linked.length, 0);

    // The failure forgot the id, so a genuine retry is still possible.
    h.CodingChat.handleMessage(note);
    await h.pump();
    assert.equal(h.count('/api/coding/sessions/attach'), 2);
});

test('the pane never registers anything without a frame', async () => {
    const h = mount();
    assert.equal(h.CodingChat.handleMessage({
        origin: 'https://code.example.com',
        source: null,
        data: { type: 'rsm.opencode.session', channel: 'ch-x', session_id: 'ses_x' },
    }), false);
    await h.flush();
    assert.equal(h.calls.requests.length, 0);
});

// ============================================================== failures
test('a refused open reports the server reason and mounts nothing', async () => {
    const h = mount();
    h.respond('/api/coding/sessions/sess-1/open', () => ({
        ok: false, status: 403, data: { code: 'coding_disabled', message: '编码功能当前未启用，请联系管理员。' },
    }));
    assert.equal(await h.run(() => h.CodingChat.open('erp-coder', 'sess-1')), false);
    assert.equal(h.frame(), null);
    assert.ok(h.calls.notes.some(m => String(m).includes('编码功能当前未启用，请联系管理员。')),
        h.calls.notes.join(' | '));
    // The platform's own pane was never disturbed.
    assert.equal(h.isHidden('chat-input-area'), false);
});

test('a silent frame offers a retry after the timeout, and the retry reopens the same session', async () => {
    const h = mount();
    h.respond('/api/coding/sessions', () => ({ ok: true, status: 200, data: PAYLOAD }));
    await h.run(() => h.CodingChat.launch('erp-coder', '/tmp/rsm/project'));
    const started = h.calls.requests.filter(r => r.path === '/api/coding/sessions')[0];
    const requestId = started.options.body.request_id;
    assert.ok(requestId, 'the create carries a request id');

    assert.equal(h.retryBox(), null, 'no retry while the frame may still be starting');
    h.clock.advance(h.CodingChat.constants.READY_TIMEOUT_MS);
    const box = h.retryBox();
    assert.ok(box, 'the pane offers a retry after waiting');
    const button = box.children.find(el => el.className === 'coding-retry-btn');
    assert.ok(button, 'the retry is an explicit control, never automatic');
    assert.equal(button.getAttribute('title'), NS.zh.coding_retry_hint);

    button.click();
    await h.pump();
    const retries = h.calls.requests.filter(r => r.path === '/api/coding/sessions');
    assert.equal(retries.length, 2, 'the retry resumes through create');
    // Same request id: the platform derives the session id from it, so this can
    // only ever be the same conversation.
    assert.equal(retries[1].options.body.request_id, requestId);
    assert.equal(retries[1].options.body.agent_id, 'erp-coder');
});

test('a coding session opened from history re-opens rather than creating', async () => {
    const h = mount();
    // A session read back from history carries no request id of its own.
    await openSession(h);
    assert.equal(h.CodingChat.current().external_session_id, PAYLOAD.external_session_id);
    assert.ok(await h.run(() => h.CodingChat.retry()));

    assert.equal(h.count('/api/coding/sessions/sess-1/open'), 2, 'the retry re-opened the same session');
    assert.equal(h.count('/api/coding/sessions'), 0, 'and never started a new one');
});

test('a session that cannot be created reports the reason and mounts nothing', async () => {
    const h = mount();
    h.respond('/api/coding/sessions', () => ({
        ok: false, status: 403, data: { message: '你的账号还没有该助手的执行权限。' },
    }));
    assert.equal(await h.run(() => h.CodingChat.launch('erp-coder', '/tmp/rsm/project')), false);
    assert.equal(h.frame(), null);
    assert.ok(h.calls.notes.some(m => String(m).includes('你的账号还没有该助手的执行权限。')));
});

// =========================================================== console seam
test('console.js routes a conversation by the Agent type', () => {
    // The pane is entered by type, so the console has to ask. These are the
    // call sites the design names: open by type, and the coding-only controls
    // the console must not offer.
    assert.match(consoleJs, /CodingChat/, 'console.js consults the coding module');
    assert.match(consoleJs, /is_coding|agent_type/, 'and knows about the Agent type field');
});

test('the i18n namespace registers every key the module asks for', () => {
    const used = Array.from(codingJs.matchAll(/codingT\('([a-z_]+)'/g)).map(m => m[1]);
    assert.ok(used.length >= 6, `expected several keys, saw ${used.length}`);
    used.forEach(key => {
        assert.ok(NS.zh[key], `zh ${key}`);
        assert.ok(NS.en[key], `en ${key}`);
    });
    // Keys referenced by name (attribute values, not calls) must exist too.
    assert.ok(NS.zh.coding_retry_hint && NS.en.coding_retry_hint);
    assert.ok(NS.zh.coding_type_coding && NS.en.coding_type_coding);
});
