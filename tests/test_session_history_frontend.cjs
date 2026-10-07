// Run the shipped history handlers against a small DOM and controlled transport.
// These cases exercise request races and search interactions that visual QA
// cannot reliably reproduce; backend tests cover title matching and permissions.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
const start = source.indexOf('// Session History (workbench page)');
const end = source.indexOf('function _toggleProjectCollapse(', start);
assert.ok(start >= 0 && end > start, 'Missing history section in console.js');
const historySource = source.slice(start, end);

function element(tagName = 'div') {
    const classes = new Set();
    let text = '', html = '';
    return {
        tagName: tagName.toUpperCase(), value: '', dataset: {}, attrs: {}, handlers: {},
        children: [], disabled: false, hidden: false, className: '',
        get firstChild() { return this.children[0] || null; },
        get textContent() { return text; },
        set textContent(value) { text = value; html = ''; this.children = []; },
        get innerHTML() { return html; },
        set innerHTML(value) { html = value; text = ''; this.children = []; },
        classList: {
            add: (...names) => names.forEach(name => classes.add(name)),
            remove: (...names) => names.forEach(name => classes.delete(name)),
            contains: name => classes.has(name),
            toggle(name, force) {
                const add = force === undefined ? !classes.has(name) : force;
                if (add) classes.add(name); else classes.delete(name);
                return add;
            },
        },
        setAttribute(key, value) { this.attrs[key] = String(value); },
        removeAttribute(key) { delete this.attrs[key]; },
        addEventListener(name, fn) { this.handlers[name] = fn; },
        appendChild(child) {
            if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(el => el !== child);
            this.children.push(child); child.parentNode = this; return child;
        },
        replaceWith(child) {
            const parent = this.parentNode;
            assert.ok(parent, 'only attached elements can be replaced');
            parent.children.splice(parent.children.indexOf(this), 1, child);
            child.parentNode = parent;
            this.parentNode = null;
        },
        replaceChildren(...children) { this.children = children; },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
        querySelectorAll(selector) {
            const matches = child => selector.startsWith('.')
                ? child.className.split(' ').includes(selector.slice(1))
                : child.tagName.toLowerCase() === selector.toLowerCase();
            return this.children.flatMap(child => [
                ...(matches(child) ? [child] : []), ...child.querySelectorAll(selector),
            ]);
        },
        focus() { this.focused = true; },
        select() { this.selected = true; },
    };
}

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

// Let fetch/json/render promise chains finish without real debounce delays.
async function settle() {
    await new Promise(resolve => setImmediate(resolve));
}

function clock() {
    let now = 0, id = 0;
    const scheduled = new Map();
    return {
        setTimeout(fn, delay) { const key = ++id; scheduled.set(key, { at: now + delay, fn }); return key; },
        clearTimeout(key) { scheduled.delete(key); },
        async advance(ms) {
            const target = now + ms;
            while (true) {
                const next = [...scheduled].filter(([, task]) => task.at <= target)
                    .sort((a, b) => a[1].at - b[1].at)[0];
                if (!next) break;
                scheduled.delete(next[0]);
                now = next[1].at;
                next[1].fn();
                await settle();
            }
            now = target;
            await settle();
        },
    };
}

const session = (id, extra = {}) => ({ session_id: id, title: id, last_active: 1700000000,
    pinned: 0, agent: { id: 'agent-b', name: 'Agent B' }, project: null, ...extra });
function response(sessions = [], query = '', extra = {}) {
    return { ok: true, status: 200, json: async () => ({ status: 'success', sessions,
        has_more: false, total: sessions.length, group_mode: 'time', project_order: [],
        ...(query ? { query } : {}), ...extra }) };
}

function setup(fetchImpl = async url => response([], new URL(url, 'http://test').searchParams.get('q') || ''), cacheStorage = new Map()) {
    const nodes = new Map();
    const storage = new Map();
    const node = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
    const calls = [];
    const time = clock();
    const ctx = vm.createContext({
        console, Date, Intl, URL, Request, Headers, URLSearchParams, AbortController,
        activeAgentId: 'agent-a', sessionId: 'current', currentView: 'history',
        window: { innerWidth: 1440, location: { href: "http://test/chat", origin: "http://test" } },
        document: { getElementById: node, createElement: element,
            querySelector: () => null, querySelectorAll: () => [] },
        localStorage: { getItem: key => cacheStorage.get(key) || null,
            setItem: (key, value) => cacheStorage.set(key, value), removeItem: key => cacheStorage.delete(key) },
        sessionStorage: { getItem: key => storage.get(key) || null },
        // The history section defers sidebar init to a microtask; the harness
        // exercises the history functions directly and has no sidebar section
        // loaded, so the deferred callback is dropped rather than run.
        queueMicrotask: () => {},
        t: key => key, escapeHtml: value => String(value), _wsAttr: value => String(value),
        _wsSelState: { current: null }, _dragSpaceKey: null,
        _sessionItemEl: s => { const el = element(); el.dataset.sessionId = s.session_id; return el; },
        _wireGroupDrag() {}, _closeSessionActionMenu() {},
        requestAnimationFrame: fn => fn(),
        setTimeout: time.setTimeout, clearTimeout: time.clearTimeout,
        fetch(url, options) { calls.push({ url, options }); return fetchImpl(url, options); },
    });
    const run = code => vm.runInContext(code, ctx);
    run(historySource);
    run('_historyVisible = true;');
    const state = () => run(`({query: _historyQuery, total: _historyTotal,
        loading: _sessionLoading, page: _sessionPage, hasMore: _sessionHasMore,
        ids: _sessionItems.map(s => s.session_id)})`);
    const input = (value, extra = {}) => {
        node('history-search-input').value = value;
        ctx.onHistorySearchInput({ target: node('history-search-input'), ...extra });
    };
    const enter = extra => ctx.onHistorySearchKeydown({ key: 'Enter', keyCode: 13,
        target: node('history-search-input'), preventDefault() {}, ...extra });
    return { ctx, run, node, calls, time, state, input, enter, storage, cacheStorage };
}

test('title search queries all accessible history, trims q and accepts server results outside the loaded list', async () => {
    const h = setup(async url => {
        const q = new URL(url, 'http://test').searchParams.get('q') || '';
        return q ? response([session('older-remote-match')], q, { total: 71, has_more: true })
            : response([session('recent-unrelated')]);
    });
    await h.ctx.loadSessionList();
    h.input('  报价 & Q3  ');
    h.enter();
    await settle();
    const params = new URL(h.calls.at(-1).url, 'http://test').searchParams;
    assert.equal(params.get('q'), '报价 & Q3');
    assert.equal(params.get('scope'), 'all');
    assert.equal(params.get('page'), '1');
    assert.equal(params.has('agent_id'), false);
    assert.deepEqual([...h.state().ids], ['older-remote-match']);
    assert.equal(h.state().total, 71);
});

function historySearchResponse(url) {
    const q = new URL(url, 'http://test').searchParams.get('q') || '';
    return response(q ? [session('ERPNext')] : [session('ERPNext'), session('unrelated')], q);
}

for (const submit of ['Enter', 'refresh']) {
    test(`${submit} synchronizes visible text when its input event was not received`, async () => {
        const h = setup(async url => historySearchResponse(url));
        await h.ctx.loadSessionList();
        // Model the observed UI: the field shows ERP, but the confirmed state
        // still contains ordinary history. Do not assume why input was missed.
        h.node('history-search-input').value = '  ERP  ';
        assert.equal(h.state().query, '');
        assert.equal(h.node('history-search-summary').textContent, '');
        assert.deepEqual([...h.state().ids], ['ERPNext', 'unrelated']);
        if (submit === 'Enter') h.enter();
        else await h.ctx.loadSessionList();
        await settle();
        assert.equal(new URL(h.calls.at(-1).url, 'http://test').searchParams.get('q'), 'ERP');
        assert.equal(h.state().query, 'ERP');
        assert.deepEqual([...h.state().ids], ['ERPNext']);
        assert.equal(h.state().total, 1);
        assert.notEqual(h.node('history-search-summary').textContent, '');
    });
}

test('Enter replaces an in-flight query when visible text changed without an input event', async () => {
    const previous = deferred();
    let calls = 0;
    const h = setup(url => ++calls === 1 ? previous.promise : Promise.resolve(historySearchResponse(url)));
    const initial = h.ctx.loadSessionList();
    h.node('history-search-input').value = 'ERP';
    h.enter();
    await settle();
    assert.equal(h.calls.length, 2, 'a new visible query must supersede the loading query');
    assert.equal(new URL(h.calls[1].url, 'http://test').searchParams.get('q'), 'ERP');
    previous.resolve(response([session('obsolete-unfiltered')]));
    await initial;
    await settle();
    assert.deepEqual([...h.state().ids], ['ERPNext']);
});

test('Enter notices an emptied search field even when no input callback updated the query', async () => {
    const h = setup(async url => historySearchResponse(url));
    h.input('ERP');
    h.enter();
    await settle();
    h.node('history-search-input').value = '';
    h.enter();
    await settle();
    assert.equal(new URL(h.calls.at(-1).url, 'http://test').searchParams.has('q'), false);
    assert.equal(h.state().query, '');
    assert.deepEqual([...h.state().ids], ['ERPNext', 'unrelated']);
    assert.equal(h.node('history-search-summary').textContent, '');
});

test('IME cancellation synchronizes restored field text without clearing it', async () => {
    const h = setup(async url => historySearchResponse(url));
    await h.ctx.loadSessionList();
    h.node('history-search-input').value = 'ERP';
    h.ctx.onHistorySearchCompositionStart();
    h.input('ERPni', { isComposing: true, inputType: 'insertCompositionText' });
    h.ctx.onHistorySearchKeydown({ key: 'Escape', keyCode: 229, isComposing: true, preventDefault() {} });
    h.node('history-search-input').value = 'ERP';
    h.ctx.onHistorySearchCompositionEnd({ target: h.node('history-search-input'), data: '' });
    await h.time.advance(300);
    assert.equal(h.node('history-search-input').value, 'ERP');
    assert.equal(h.state().query, 'ERP');
    assert.deepEqual([...h.state().ids], ['ERPNext']);
});

test('a non-composing input can recover a stale composition marker', async () => {
    const h = setup(async url => historySearchResponse(url));
    await h.ctx.loadSessionList();
    h.ctx.onHistorySearchCompositionStart();
    // If compositionend was not delivered, a later ordinary InputEvent still
    // tells us composition is over. Its concrete value must not stay ignored.
    h.input('ERP', { isComposing: false, inputType: 'insertText' });
    await h.time.advance(300);
    assert.equal(h.state().query, 'ERP');
    assert.deepEqual([...h.state().ids], ['ERPNext']);
    assert.equal(h.run('_historySearchComposing'), false);
});

test('an explicitly non-composing Enter recovers a stale composition marker and submits visible text', async () => {
    const h = setup(async url => historySearchResponse(url));
    await h.ctx.loadSessionList();
    h.ctx.onHistorySearchCompositionStart();
    h.node('history-search-input').value = 'ERP';
    h.enter({ isComposing: false, keyCode: 13 });
    await settle();
    assert.equal(new URL(h.calls.at(-1).url, 'http://test').searchParams.get('q'), 'ERP');
    assert.equal(h.run('_historySearchComposing'), false);
    assert.deepEqual([...h.state().ids], ['ERPNext']);
});

test('IME confirmation keyCode 229 does not submit partial text even if isComposing is false', async () => {
    const h = setup(async url => historySearchResponse(url));
    h.ctx.onHistorySearchCompositionStart();
    h.input('ERPni', { isComposing: true, inputType: 'insertCompositionText' });
    h.enter({ isComposing: false, keyCode: 229 });
    await h.time.advance(500);
    assert.equal(h.calls.length, 0);
    assert.equal(h.run('_historySearchComposing'), true);
    h.node('history-search-input').value = 'ERP';
    h.ctx.onHistorySearchCompositionEnd({ target: h.node('history-search-input'), data: 'ERP' });
    await h.time.advance(300);
    assert.equal(h.calls.length, 1);
    assert.equal(new URL(h.calls[0].url, 'http://test').searchParams.get('q'), 'ERP');
});

test('change commits visible text and recovers an unfinished composition marker', async () => {
    const h = setup(async url => historySearchResponse(url));
    await h.ctx.loadSessionList();
    h.ctx.onHistorySearchCompositionStart();
    h.node('history-search-input').value = 'ERP';
    h.ctx.onHistorySearchChange({ type: 'change', target: h.node('history-search-input') });
    await settle();
    assert.equal(new URL(h.calls.at(-1).url, 'http://test').searchParams.get('q'), 'ERP');
    assert.equal(h.state().query, 'ERP');
    assert.deepEqual([...h.state().ids], ['ERPNext']);
    assert.equal(h.run('_historySearchComposing'), false);
});

test('the native search clear event restores all history without depending on the custom clear button', async () => {
    const h = setup(async url => historySearchResponse(url));
    h.input('ERP');
    h.enter();
    await settle();
    h.node('history-search-input').value = '';
    h.ctx.onHistorySearchChange({ type: 'search', target: h.node('history-search-input') });
    await settle();
    assert.equal(new URL(h.calls.at(-1).url, 'http://test').searchParams.has('q'), false);
    assert.equal(h.state().query, '');
    assert.deepEqual([...h.state().ids], ['ERPNext', 'unrelated']);
});

test('change submits pending debounce immediately but deduplicates in-flight and completed queries', async () => {
    const pending = deferred();
    const h = setup(() => pending.promise);
    h.input('ERP');
    assert.equal(h.calls.length, 0);
    h.ctx.onHistorySearchChange({ type: 'change', target: h.node('history-search-input') });
    assert.equal(h.calls.length, 1);
    h.ctx.onHistorySearchChange({ type: 'search', target: h.node('history-search-input') });
    await h.time.advance(500);
    assert.equal(h.calls.length, 1);
    pending.resolve(response([session('ERPNext')], 'ERP'));
    await settle();
    h.ctx.onHistorySearchChange({ type: 'change', target: h.node('history-search-input') });
    await settle();
    assert.equal(h.calls.length, 1);
});

test('typing debounces for 300 ms from the last input; Enter submits once immediately', async () => {
    const h = setup();
    h.input('e');
    await h.time.advance(200);
    h.input('erp');
    await h.time.advance(299);
    assert.equal(h.calls.length, 0);
    await h.time.advance(1);
    assert.equal(h.calls.length, 1);
    h.input('ERPNext');
    h.enter();
    await settle();
    assert.equal(h.calls.length, 2);
    assert.equal(new URL(h.calls[1].url, 'http://test').searchParams.get('q'), 'ERPNext');
    await h.time.advance(500);
    assert.equal(h.calls.length, 2);
});

test('Chinese composition and its confirmation Enter do not issue partial searches', async () => {
    const h = setup();
    h.ctx.onHistorySearchCompositionStart();
    h.input('hui', { isComposing: true });
    h.enter({ isComposing: true, keyCode: 229 });
    await h.time.advance(500);
    assert.equal(h.calls.length, 0);
    h.node('history-search-input').value = '会议';
    h.ctx.onHistorySearchCompositionEnd({ target: h.node('history-search-input') });
    // Real browsers dispatch a final non-composing input after compositionend.
    h.input('会议');
    await h.time.advance(300);
    assert.equal(h.calls.length, 1);
    assert.equal(new URL(h.calls[0].url, 'http://test').searchParams.get('q'), '会议');
});

test('new input immediately invalidates an older result during the debounce interval', async () => {
    const old = deferred();
    const h = setup(() => old.promise);
    h.input('old');
    h.enter();
    h.input('new');
    old.resolve(response([session('obsolete')], 'old'));
    await settle();
    assert.equal(h.calls.length, 1, 'new query should still be debouncing');
    assert.deepEqual([...h.state().ids], []);
});

for (const failure of [false, true]) {
    test(`a stale ${failure ? 'failure' : 'success'} cannot end the newer request loading state`, async () => {
        const old = deferred(), fresh = deferred();
        let count = 0;
        const h = setup(() => (++count === 1 ? old.promise : fresh.promise));
        h.input('old');
        h.enter();
        h.input('fresh');
        h.enter();
        h.enter();
        assert.equal(h.calls.length, 2);
        assert.equal(h.state().loading, true);
        if (failure) old.reject(Error('old request failed'));
        else old.resolve(response([session('obsolete')], 'old'));
        await settle();
        assert.equal(h.state().loading, true);
        assert.deepEqual([...h.state().ids], []);
        assert.equal(h.node('history-status').classList.contains('history-status-error'), false);
        fresh.resolve(response([session('fresh-result')], 'fresh'));
        await settle();
        assert.equal(h.state().loading, false);
        assert.deepEqual([...h.state().ids], ['fresh-result']);
    });
}

test('clearing search cancels the pending query and reloads page one without q', async () => {
    const pending = deferred();
    let count = 0;
    const h = setup(() => (++count === 1 ? pending.promise : Promise.resolve(response([session('restored')]))));
    h.input('采购');
    h.enter();
    h.ctx.clearHistorySearch();
    await settle();
    assert.equal(h.calls.length, 2);
    const params = new URL(h.calls[1].url, 'http://test').searchParams;
    assert.equal(params.has('q'), false);
    assert.equal(params.get('page'), '1');
    assert.equal(h.node('history-search-input').value, '');
    assert.equal(h.state().query, '');
    assert.deepEqual([...h.state().ids], ['restored']);
    pending.resolve(response([session('late-search-result')], '采购'));
    await settle();
    await h.time.advance(500);
    assert.equal(h.calls.length, 2);
    assert.deepEqual([...h.state().ids], ['restored']);
});

test('a search response without the matching query echo is rejected instead of showing unfiltered history', async () => {
    for (const echoedQuery of ['', 'different']) {
        const h = setup(async () => response([session('unfiltered-private-title')], echoedQuery));
        h.input('wanted');
        h.enter();
        await settle();
        assert.deepEqual([...h.state().ids], []);
        assert.equal(h.state().loading, false);
        assert.equal(h.node('history-status').classList.contains('history-status-error'), true);
    }
});

test('zero matches has a search empty state; whitespace restores ordinary history', async () => {
    const h = setup(async url => {
        const q = new URL(url, 'http://test').searchParams.get('q') || '';
        return q ? response([], q) : response([session('ordinary')]);
    });
    h.input('missing');
    h.enter();
    await settle();
    assert.equal(h.state().total, 0);
    assert.equal(h.node('history-status').textContent, 'history_search_empty');
    assert.equal(h.node('history-status').classList.contains('history-status-error'), false);
    h.input(' \t ');
    await settle();
    assert.equal(new URL(h.calls.at(-1).url, 'http://test').searchParams.has('q'), false);
    assert.deepEqual([...h.state().ids], ['ordinary']);
    assert.equal(h.node('history-status').textContent, '');
});

test('identity reset clears query and results, and a late tenant response cannot restore them', async () => {
    const pending = deferred();
    const h = setup(() => pending.promise);
    h.storage.set('cow_tenant_id', 'old-tenant');
    h.input('old-tenant-search');
    h.enter();
    h.storage.set('cow_tenant_id', 'new-tenant');
    h.ctx._resetHistorySearch();
    pending.resolve(response([session('old-tenant-session')], 'old-tenant-search'));
    await settle();
    assert.equal(h.state().query, '');
    assert.equal(h.node('history-search-input').value, '');
    assert.equal(h.state().total, null);
    assert.equal(h.state().loading, false);
    assert.deepEqual([...h.state().ids], []);
});

test('query length counts Unicode characters and Enter cannot bypass the 100-character limit', async () => {
    const h = setup();
    const longest = '搜'.repeat(99) + '😀';
    h.input(longest);
    h.enter();
    await settle();
    assert.equal(h.calls.length, 1);
    assert.equal(new URL(h.calls[0].url, 'http://test').searchParams.get('q'), longest);
    h.input(longest + '多');
    h.enter();
    await h.time.advance(500);
    assert.equal(h.calls.length, 1);
    assert.equal(h.node('history-status').textContent, 'history_search_limit');
    assert.equal(h.node('history-status').classList.contains('history-status-error'), true);
});

test('failed pagination preserves confirmed rows and retry requests the same page and query', async () => {
    let calls = 0;
    const h = setup(async () => {
        calls++;
        if (calls === 1) return response([session('first')], 'match', { has_more: true, total: 2 });
        if (calls === 2) throw Error('offline');
        return response([session('second')], 'match', { total: 2 });
    });
    h.input('match');
    h.enter();
    await settle();
    await h.run('_fetchSessionPage(2, false, undefined, _sessionReqSeq)');
    await settle();
    assert.equal(h.state().page, 1);
    assert.equal(h.state().loading, false);
    assert.deepEqual([...h.state().ids], ['first']);
    const retry = h.node('history-status').children.find(child => child.tagName === 'BUTTON');
    assert.ok(retry, 'pagination failure must expose a retry action');
    retry.handlers.click();
    await settle();
    const params = new URL(h.calls.at(-1).url, 'http://test').searchParams;
    assert.equal(params.get('page'), '2');
    assert.equal(params.get('q'), 'match');
    assert.equal(h.state().page, 2);
    assert.equal(h.state().hasMore, false);
    assert.deepEqual([...h.state().ids], ['first', 'second']);
});

for (const { pagination, failed } of [
    { pagination: false, failed: false }, { pagination: false, failed: true },
    { pagination: true, failed: false }, { pagination: true, failed: true },
]) {
    test(`a late ${pagination ? 'pagination' : 'refresh'} ${failed ? 'failure' : 'response'} cannot remove a rename draft`, async () => {
        const pending = deferred();
        let calls = 0;
        const h = setup(() => (++calls === 1
            ? Promise.resolve(response([session('first')], '', { has_more: true, total: 2 }))
            : pending.promise));
        await h.ctx.loadSessionList();
        const request = pagination
            ? h.run('_fetchSessionPage(2, false, undefined, _sessionReqSeq)')
            : h.ctx.loadSessionList();
        const list = h.node('session-list');
        const draft = element('input');
        draft.value = 'Uncommitted title';
        list.appendChild(draft);
        list.querySelector = selector => selector === '.session-title-input' ? draft : null;
        if (failed) pending.reject(Error('offline'));
        else pending.resolve(response([session('second')]));
        await request;
        await settle();
        assert.equal(list.children.includes(draft), true, 'rendering must not detach the focused editor');
        assert.equal(draft.value, 'Uncommitted title');
        assert.equal(h.state().loading, false);
        assert.equal(h.run('_historyDirty'), true, 'closing the editor must revalidate the deferred list');
    });
}

function renameFixture() {
    const h = setup(async () => response());
    const from = source.indexOf('function renameSession(');
    const to = source.indexOf('function deleteSession(', from);
    assert.ok(from >= 0 && to > from, 'Missing rename handler');
    h.run(source.slice(from, to));
    const rows = ['agent-a', 'agent-b'].map(owner => {
        const row = element(); row.className = 'session-item';
        row.dataset = { sessionId: 'shared/id', agentId: owner };
        const button = element('button'); button.className = 'session-row-main';
        const line = element('span'); line.className = 'session-title-line';
        const title = element('span'); title.className = 'session-title'; title.textContent = owner + ' old title';
        line.appendChild(title); button.appendChild(line); row.appendChild(button);
        return row;
    });
    h.ctx.document.querySelectorAll = selector => selector === '.session-item' ? rows : [];
    h.ctx.fixtures = rows.map(row => session(row.dataset.sessionId, {
        agent: { id: row.dataset.agentId }, title: row.querySelector('.session-title').textContent,
    }));
    h.run('_sessionItems = fixtures;');
    let refreshes = 0;
    h.ctx._refreshHistoryList = () => { refreshes++; };
    return { ...h, rows, refreshes: () => refreshes };
}

test('rename edits the owning Agent row, restores its original button and submits only once on Enter then blur', async () => {
    const h = renameFixture();
    const target = h.rows[1];
    const originalButton = target.querySelector('.session-row-main');
    h.ctx.renameSession('shared/id', 'agent-b');
    const input = target.querySelector('.session-title-input');
    assert.ok(input);
    assert.equal(target.querySelector('.session-row-main').tagName, 'DIV');
    assert.equal(h.rows[0].querySelector('.session-title').textContent, 'agent-a old title');
    input.value = '  New owner title  ';
    input.handlers.keydown({ key: 'Enter', preventDefault() {} });
    input.handlers.blur();
    await settle();
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].url, '/api/sessions/shared%2Fid?agent_id=agent-b');
    assert.deepEqual(JSON.parse(h.calls[0].options.body), { title: 'New owner title', agent_id: 'agent-b' });
    assert.equal(target.querySelector('.session-row-main'), originalButton);
    assert.equal(target.querySelector('.session-title').textContent, 'New owner title');
    assert.equal(target.querySelector('.session-title-input'), null);
    assert.equal(h.refreshes(), 0, 'an ordinary rename updates the row without reloading page one');
});

test('renaming a later-page row retains loaded sessions and scroll position', async () => {
    const h = renameFixture();
    h.ctx.fixtures = Array.from({length:61}, (_,i)=>session('older-'+i)).concat(h.ctx.fixtures);
    h.run('_sessionItems = fixtures; _sessionPage = 2;');
    h.node('session-list').scrollTop = 1700;
    h.ctx.renameSession('shared/id', 'agent-b');
    const input = h.rows[1].querySelector('.session-title-input');
    input.value = 'renamed on page two'; input.handlers.blur(); await settle();
    assert.equal(h.refreshes(), 0);
    assert.equal(h.run('_sessionItems.length'), 63);
    assert.equal(h.run('_sessionPage'), 2);
    assert.equal(h.node('session-list').scrollTop, 1700);
});

test('renaming search results still revalidates matches and deferred refreshes are not lost', async () => {
    for (const state of ["_historyQuery = 'old title'", '_historyDirty = true']) {
        const h = renameFixture(); h.run(state);
        h.ctx.renameSession('shared/id', 'agent-b');
        const input = h.rows[1].querySelector('.session-title-input');
        input.value = 'changed'; input.handlers.blur(); await settle();
        assert.equal(h.refreshes(), 1);
    }
});

test('rename ignores IME Enter and Escape restores the title without a blur save', async () => {
    const h = renameFixture();
    const target = h.rows[1];
    const originalButton = target.querySelector('.session-row-main');
    h.ctx.renameSession('shared/id', 'agent-b');
    const input = target.querySelector('.session-title-input');
    input.value = '组合输入草稿';
    input.handlers.keydown({ key: 'Enter', isComposing: true, keyCode: 229, preventDefault() {} });
    assert.equal(h.calls.length, 0);
    assert.equal(target.querySelector('.session-title-input'), input);
    input.handlers.keydown({ key: 'Escape', preventDefault() {} });
    input.handlers.blur();
    await settle();
    assert.equal(h.calls.length, 0);
    assert.equal(target.querySelector('.session-row-main'), originalButton);
    assert.equal(target.querySelector('.session-title').textContent, 'agent-b old title');
});

function sourceSection(from, to) {
    const start = source.indexOf(from), end = source.indexOf(to, start);
    assert.ok(start >= 0 && end > start, `Missing source section: ${from}`);
    return source.slice(start, end);
}

function resumeFixture(transport) {
    const h = setup();
    const requests = [], preferences = new Map(), rows = [];
    const messages = element(), input = element('textarea');
    messages.insertBefore = fragment => messages.children.unshift(...fragment.children);
    const ctx = h.ctx;
    Object.assign(ctx, {
        Headers, Request, _authEpoch: 1, defaultAgentId: 'agent-a', currentLang: 'zh',
        _sessCfg: { team: [] }, historyPage: 0, historyHasMore: false, historyLoading: false,
        _historyLoadSeq: 0, _historyLoadContext: '',
        messagesDiv: messages, chatInput: input, pendingAttachments: [], inputHistory: [],
        historyIdx: -1, historySavedDraft: '', sendBtn: { disabled: true },
        sessionActiveRequest: {}, loadingContainers: {},
        writeScopedPreference: (key, value) => preferences.set(key, value),
        _identityMode: () => 'database',
        _wsSelUpdateLabel() {}, updateEditButtonsState() {}, refreshWorkspaceSelector() {},
        refreshSessionSettings: async () => { ctx._sessCfg = { team: [] }; },
        wsOnSessionSwitch() {}, wsGuardUnsaved: () => true, startPolling() {},
        _desktopContextClear() {}, _desktopRestoreContext: async () => {},
        _desktopContextForRequest: () => null,
        setSendBtnCancelMode() {}, _reattachStream() {}, renderComposerIdentity() {},
        resetSendBtnSendMode() { ctx.sendBtn.disabled = false; },
        navigateTo(view) { ctx.currentView = view; },
        createUserMessageEl(text) { const el = element(); el.textContent = text; return el; },
        createBotMessageEl(text) { const el = element(); el.textContent = text; return el; },
        flushPendingVoiceAttachments() {}, scrollChatToBottom() {},
        syncTeamFromText() {}, resetComposerHeight() {}, renderAttachmentPreview() {},
        addressedAgentId: () => '', addLoadingIndicator: () => ({ remove() {} }),
        addUserMessage(text) { messages.appendChild(ctx.createUserMessageEl(text)); },
        addBotMessage(text) { messages.appendChild(ctx.createBotMessageEl(text)); },
        readMessageResponse: response => response.json(), rememberLiveSpeaker() {}, setLoadingSpeaker() {},
        _historyTimeLabel: () => ({ text: '', full: '' }), agentAvatarHTML: () => '',
    });
    h.storage.set('cow_tenant_id', 'tenant-one');
    ctx.document.getElementById = id => id === 'chat-input' ? input : null;
    ctx.document.querySelectorAll = selector => selector === '.session-item' ? rows : [];
    ctx.document.createDocumentFragment = () => element();
    ctx.window.fetch = async (url, options) => {
        requests.push({ url, options });
        if (transport) return transport(url, options);
        return { ok: true, status: 200, json: async () => url.startsWith('/api/history')
            ? { status: 'success', messages: [{ role: 'user', content: 'Previous question', created_at: 1, _seq: 1 },
                { role: 'assistant', content: 'Previous answer', created_at: 2, _seq: 2 }], has_more: false }
            : { status: 'success', inline_reply: 'Continued answer' } };
    };
    h.run(sourceSection('function runtimeSessionKey(', 'function isCurrentSessionConversationActive('));
    h.run('const SESSION_ID_KEY = "cow_session_id";');
    h.run(sourceSection('function activeSessionStorageKey(', 'function generateSessionId('));
    ctx.fetch = (...args) => ctx.window.fetch(...args);
    h.run(sourceSection('function focusChatComposer(', '// When a card\'s target'));
    h.run(sourceSection('function handoffPayload(', 'function reloadHistoryView('));
    h.run(sourceSection('function loadHistory(', 'function addLoadingIndicator('));
    h.run(sourceSection('function switchSession(', '// In-place rename a session title'));
    h.run(sourceSection('function sendMessage(', 'function startSSE('));
    h.run(sourceSection('function _sessionItemEl(', '// Pin / unpin'));
    const row = (sid, agent) => {
        const item = element(), button = element('button'), more = element('button');
        item.querySelector = selector => selector === '.session-row-main' ? button : more;
        const create = ctx.document.createElement;
        ctx.document.createElement = () => item;
        try { ctx._sessionItemEl(session(sid, { agent: { id: agent } })); }
        finally { ctx.document.createElement = create; }
        rows.push(item);
        return { item, click: () => button.handlers.click() };
    };
    return { ...h, requests, preferences, messages, composer: input, row };
}

test('clicking history restores the original Agent and messages, focuses the composer and continues the same session', async () => {
    const h = resumeFixture();
    h.row('historical/session', 'erpnext').click();
    await settle();
    assert.equal(h.ctx.currentView, 'chat');
    assert.equal(h.ctx.activeAgentId, 'erpnext');
    assert.equal(h.ctx.sessionId, 'historical/session');
    assert.equal(h.preferences.get('cow_session_id:erpnext'), 'historical/session');
    assert.deepEqual(h.messages.children.map(el => el.textContent), ['Previous question', 'Previous answer']);
    const history = new URL(h.requests[0].url, 'http://test');
    assert.equal(history.searchParams.get('agent_id'), 'erpnext');
    assert.equal(history.searchParams.get('session_id'), 'historical/session');
    assert.equal(h.composer.focused, true);
    assert.equal(h.ctx.sendBtn.disabled, false);
    h.composer.value = 'Continue this conversation';
    h.ctx.sendMessage();
    await settle();
    const send = h.requests.find(call => call.url.startsWith('/message'));
    assert.equal(new URL(send.url, 'http://test').searchParams.get('agent_id'), 'erpnext');
    assert.equal(send.options.headers.get('X-Tenant-ID'), 'tenant-one');
    const body = JSON.parse(send.options.body);
    assert.equal(body.session_id, 'historical/session');
    assert.equal(body.agent_id, 'erpnext');
    assert.equal(body.message, 'Continue this conversation');
    assert.deepEqual(h.messages.children.map(el => el.textContent),
        ['Previous question', 'Previous answer', 'Continue this conversation', 'Continued answer']);
});

test('same session ID under a different Agent reloads its conversation and marks only the owning row active', async () => {
    const h = resumeFixture();
    const old = h.row('current', 'agent-a'), target = h.row('current', 'agent-b');
    target.click();
    await settle();
    assert.equal(h.ctx.activeAgentId, 'agent-b');
    assert.equal(h.requests.length, 1);
    assert.equal(h.preferences.get('cow_session_id:agent-b'), 'current');
    assert.equal(old.item.classList.contains('active'), false);
    assert.equal(target.item.classList.contains('active'), true);
    assert.deepEqual(h.messages.children.map(el => el.textContent), ['Previous question', 'Previous answer']);
});

test('canceling unsaved edits preserves the original Agent, conversation and view', async () => {
    const h = resumeFixture();
    h.ctx.wsGuardUnsaved = () => false;
    h.row('historical/session', 'erpnext').click();
    await settle();
    assert.equal(h.ctx.currentView, 'history');
    assert.equal(h.ctx.activeAgentId, 'agent-a');
    assert.equal(h.ctx.sessionId, 'current');
    assert.equal(h.requests.length, 0);
    assert.equal(h.preferences.size, 0);
});

const messageResponse = text => ({ ok: true, status: 200, json: async () => ({
    status: 'success', messages: [{ role: 'user', content: text, created_at: 1 }], has_more: false,
}) });

test('a history load failure is visible and clicking the same session retries it', async () => {
    let reads = 0;
    const h = resumeFixture(() => ++reads === 1
        ? { ok: false, status: 500, json: async () => ({ status: 'error' }) }
        : messageResponse('Recovered history'));
    const notices = [];
    h.ctx._wsToast = notice => notices.push(notice);
    h.ctx.console = { ...console, warn() {} };
    const target = h.row('previous', 'erpnext');
    target.click();
    await settle();
    assert.deepEqual(notices, ['session_history_failed']);
    assert.equal(h.ctx.historyLoading, false);
    h.ctx.currentView = 'history';
    target.click();
    await settle();
    assert.equal(reads, 2);
    assert.equal(h.ctx.currentView, 'chat');
    assert.deepEqual(h.messages.children.map(el => el.textContent), ['Recovered history']);
});

test('switching Agent while settings load never fetches the old session with the new Agent', async () => {
    const settings = deferred();
    const h = resumeFixture();
    h.ctx.refreshSessionSettings = () => settings.promise;
    h.row('first-session', 'agent-a').click();
    h.row('second-session', 'agent-b').click();
    settings.resolve();
    await settle();
    assert.equal(h.requests.length, 1);
    const params = new URL(h.requests[0].url, 'http://test').searchParams;
    assert.equal(params.get('session_id'), 'second-session');
    assert.equal(params.get('agent_id'), 'agent-b');
});

test('a late response from another Agent cannot render or finish the current history load', async () => {
    const first = deferred(), second = deferred();
    const h = resumeFixture(url => new URL(url, 'http://test').searchParams.get('agent_id') === 'agent-a'
        ? first.promise : second.promise);
    h.row('same-session', 'agent-a').click();
    await settle();
    h.row('same-session', 'agent-b').click();
    await settle();
    first.resolve(messageResponse('Other Agent history'));
    await settle();
    assert.equal(h.ctx.historyLoading, true);
    assert.equal(h.messages.children.length, 0);
    second.resolve(messageResponse('Chosen Agent history'));
    await settle();
    assert.equal(h.ctx.historyLoading, false);
    assert.deepEqual(h.messages.children.map(el => el.textContent), ['Chosen Agent history']);
});

test('tenant changes allow a fresh history read while the previous tenant request is pending', async () => {
    const first = deferred(), second = deferred();
    const h = resumeFixture((url, options) => options.headers.get('X-Tenant-ID') === 'tenant-one'
        ? first.promise : second.promise);
    h.ctx.loadHistory(1);
    await settle();
    h.storage.set('cow_tenant_id', 'tenant-two');
    h.ctx._authEpoch++;
    h.ctx.loadHistory(1);
    await settle();
    assert.equal(h.requests.length, 2);
    first.resolve(messageResponse('Previous tenant history'));
    await settle();
    assert.equal(h.messages.children.length, 0);
    assert.equal(h.ctx.historyLoading, true);
    second.resolve(messageResponse('Selected tenant history'));
    await settle();
    assert.deepEqual(h.messages.children.map(el => el.textContent), ['Selected tenant history']);
    assert.equal(h.ctx.historyLoading, false);
});

test('history reconnect carries the same identity and Agent ownership as a live stream', async () => {
    const h = resumeFixture(async () => ({ ok: true, status: 200, json: async () => ({
        status: 'success', messages: [{ role: 'user', content: 'In progress', created_at: 1, _seq: 1 }],
        active_request: { request_id: 'pending-reply', after_seq: 7 }, has_more: false,
    }) }));
    const calls = [];
    h.ctx.resumedRequests = new Set();
    h.ctx.startSSE = (...args) => calls.push(args);
    await h.ctx.loadHistory(1);
    assert.equal(calls.length, 1);
    const owner = calls[0][5];
    assert.equal(owner.authEpoch, 1);
    assert.equal(owner.tenantId, 'tenant-one');
    assert.equal(owner.agentId, 'agent-a');
    assert.equal(owner.sid, 'current');
    assert.equal(calls[0][6].afterSeq, 7);
    await h.ctx.loadHistory(1);
    assert.equal(calls.length, 1, 'one active request is resumed only once');
});

test('panel drops the page search and stale response; returning restores text from page one', async () => {
    const pending=deferred();
    const h=setup(async url => url.includes('q=kept') ? pending.promise : response([session('normal')]));
    let surface='page'; h.ctx.sessionHistorySurface=()=>surface;
    h.node('history-search-input').value='kept';h.ctx.loadSessionList();
    surface='panel';h.ctx._cancelHistoryRequest();await h.ctx.loadSessionList();
    pending.resolve(response([session('late-search')],'kept'));await settle();
    assert.equal(h.state().query,'');assert.deepEqual([...h.state().ids],['normal']);
    assert.equal(h.node('history-search-input').value,'kept');
    surface='page';h.ctx._cancelHistoryRequest();await h.ctx.loadSessionList();
    assert.equal(h.state().query,'kept');assert.ok(h.calls.at(-1).url.includes('page=1'));
});
test('panel pagination reaches more than 50 rows with the same owner-aware list', async () => {
    const h=setup(async url=>response(Array.from({length:url.includes('page=2')?13:50},(_,i)=>session('s'+(url.includes('page=2')?50+i:i))), '', {has_more:!url.includes('page=2')}));
    h.ctx.sessionHistorySurface=()=> 'panel';await h.ctx.loadSessionList();
    assert.equal(h.state().ids.length,50);
    await h.run('_fetchSessionPage(2, false, undefined, _sessionReqSeq)');
    assert.equal(h.state().ids.length,63);assert.equal(h.state().hasMore,false);
});
test('temporary row survives an empty refresh, saved same-owner row replaces it once, switch drops it',async()=>{
    let saved=false;
    const h=setup(async ()=>response(saved?[session('current',{agent:{id:'agent-a'},title:'Saved'})]:[]));
    h.ctx._sessCfg=null;h.ctx.sessionHistorySurface=()=> 'panel';
    h.ctx._addOptimisticSessionItem('current');
    await h.ctx.loadSessionList();assert.equal(h.run('_sessionItems[0].optimistic'),true);
    saved=true;await h.ctx.loadSessionList();
    assert.equal(h.state().ids.length,1);assert.equal(h.run('_sessionItems[0].optimistic'),undefined);
    h.ctx.sessionId='new';h.ctx._addOptimisticSessionItem('new');
    h.ctx.sessionId='different';await h.ctx.loadSessionList();
    assert.equal(h.run('_sessionItems.some(s=>s.optimistic)'),false);
});

test('silent refresh keeps confirmed rows and does not show the loading status', async () => {
    const pending = deferred();
    let calls = 0;
    const h = setup(url => {
        calls += 1;
        if (calls === 1) return Promise.resolve(response([session('a'), session('b')], '', { total: 2 }));
        return pending.promise;
    });
    await h.ctx.loadSessionList();
    assert.equal(h.node('history-status').textContent, '');
    h.ctx._refreshHistoryList();
    await settle();
    assert.equal(h.calls.length, 2);
    assert.deepEqual([...h.state().ids], ['a', 'b']);
    assert.notEqual(h.node('history-status').textContent, 'session_history_loading');
    pending.resolve(response([session('a', { title: 'Renamed' }), session('b')], '', { total: 2 }));
    await settle();
    assert.equal(h.run('_sessionItems[0].title'), 'Renamed');
    assert.equal(h.node('history-status').textContent, '');
});

test('a silent refresh queues behind an in-flight list read instead of aborting it', async () => {
    const first = deferred();
    let calls = 0;
    const h = setup(url => ++calls === 1
        ? first.promise
        : Promise.resolve(response([session('second')], '', { total: 1 })));
    const loading = h.ctx.loadSessionList();
    await settle();
    h.ctx._refreshHistoryList();
    h.ctx._refreshHistoryList();
    await settle();
    assert.equal(h.calls.length, 1, 'queued silent reads must not abort the visible load');
    first.resolve(response([session('first')], '', { total: 1 }));
    await loading;
    await settle();
    assert.equal(h.calls.length, 2);
    assert.deepEqual([...h.state().ids], ['second']);
});

function authenticateHistory(h, username = 'alice', tenant = 'team-a') {
    h.ctx._accountState = { authenticated: true, username };
    h.ctx._accountAppVisible = true;
    h.storage.set('cow_tenant_id', tenant);
}

test('restart paints cached summaries before a slow refresh, then replaces them with server rows', async () => {
    const cache = new Map();
    const initial = setup(async () => response([session('cached', { content: 'must not persist' })]), cache);
    authenticateHistory(initial);
    await initial.ctx.loadSessionList();
    assert.equal(cache.size, 1);
    assert.equal([...cache.values()][0].includes('must not persist'), false);
    const slow = deferred();
    const restarted = setup(() => slow.promise, cache);
    authenticateHistory(restarted);
    const loading = restarted.ctx.loadSessionList();
    assert.deepEqual([...restarted.state().ids], ['cached']);
    assert.equal(restarted.node('history-status').textContent, '');
    slow.resolve(response([session('fresh')]));
    await loading;
    assert.deepEqual([...restarted.state().ids], ['fresh']);
    assert.equal(JSON.parse([...cache.values()][0]).sessions[0].session_id, 'fresh');
});

test('cached summaries cannot cross server, account or tenant boundaries or appear before login', async () => {
    const cache = new Map();
    const first = setup(async () => response([session('private')]), cache);
    authenticateHistory(first);
    await first.ctx.loadSessionList();
    for (const variant of ['server', 'account', 'tenant', 'signed-out', 'not-ready', 'denied']) {
        const h = setup(async () => response([]), cache);
        authenticateHistory(h);
        if (variant === 'server') h.ctx.window.location.origin = 'https://other.example';
        if (variant === 'account') h.ctx._accountState.username = 'bob';
        if (variant === 'tenant') h.storage.set('cow_tenant_id', 'team-b');
        if (variant === 'signed-out') h.ctx._accountState.authenticated = false;
        if (variant === 'not-ready') h.ctx._accountAppVisible = false;
        if (variant === 'denied') h.ctx._viewNavDenied = () => true;
        h.ctx._restoreHistoryCache();
        assert.deepEqual([...h.state().ids], [], variant);
    }
});

test('reopening during a slow read shares the request; fresh surface switches do not refetch', async () => {
    const slow = deferred();
    const h = setup(() => slow.promise);
    const first = h.ctx.loadSessionList();
    const second = h.ctx.loadSessionList(undefined, { revalidate: true });
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].options.signal.aborted, false);
    slow.resolve(response([session('loaded')]));
    await Promise.all([first, second]);
    await h.ctx.loadSessionList(undefined, { revalidate: true });
    assert.equal(h.calls.length, 1);
});

test('timeout settles even an unresponsive transport, keeps cached rows and allows retry', async () => {
    const h = setup(async () => response([session('kept')]));
    authenticateHistory(h);
    await h.ctx.loadSessionList();
    let requests = 0;
    h.ctx.fetch = () => ++requests === 1 ? new Promise(() => {}) : Promise.resolve(response([session('recovered')]));
    const loading = h.ctx.loadSessionList();
    await h.time.advance(15000);
    await loading;
    assert.equal(h.state().loading, false);
    assert.deepEqual([...h.state().ids], ['kept']);
    assert.equal(h.run('_historyStatus.error'), true);
    await h.run('_historyStatus.retry()');
    assert.deepEqual([...h.state().ids], ['recovered']);
});

test('authorization failure discards cached rows, including a non-JSON denial response', async () => {
    const h = setup(async () => response([session('private')]));
    authenticateHistory(h);
    await h.ctx.loadSessionList();
    h.ctx.fetch = async () => ({ status: 403, json: async () => { throw Error('HTML error page'); } });
    await h.ctx.loadSessionList();
    assert.deepEqual([...h.state().ids], []);
    assert.equal(h.cacheStorage.size, 0);
});

test('corrupt, expired or unavailable storage never blocks a successful history read', async () => {
    for (const variant of ['corrupt', 'expired', 'blocked']) {
        const h = setup(async () => response([session('fresh')]));
        authenticateHistory(h);
        const key = h.run('_historyCacheKey()');
        h.cacheStorage.set(key, variant === 'corrupt' ? '{broken' : JSON.stringify({
            savedAt: 1, sessions: [session('stale')], total: 1, project_order: [],
        }));
        if (variant === 'blocked') h.ctx.localStorage.getItem = () => { throw Error('storage disabled'); };
        h.ctx._restoreHistoryCache();
        assert.deepEqual([...h.state().ids], []);
        await h.ctx.loadSessionList();
        assert.deepEqual([...h.state().ids], ['fresh']);
    }
});

test('search results never replace the restart cache and late account results never populate it', async () => {
    const h = setup(async url => response([session(url.includes('q=') ? 'search' : 'recent')],
        new URL(url, 'http://test').searchParams.get('q') || ''));
    authenticateHistory(h);
    await h.ctx.loadSessionList();
    const cached = [...h.cacheStorage.values()][0];
    h.node('history-search-input').value = 'query';
    await h.ctx.loadSessionList();
    assert.equal([...h.cacheStorage.values()][0], cached);
    const slow = deferred();
    h.ctx.fetch = () => slow.promise;
    h.node('history-search-input').value = '';
    const loading = h.ctx.loadSessionList();
    h.ctx._resetHistorySearch();
    h.ctx._accountState.username = 'bob';
    slow.resolve(response([session('old-account')]));
    await loading;
    assert.equal(h.cacheStorage.size, 1);
    assert.equal([...h.cacheStorage.values()][0], cached);
    assert.deepEqual([...h.state().ids], []);
});

test('confirmed row updates patch the restart cache and hidden-list mutations invalidate it', async () => {
    const h = setup(async () => response([session('existing')]));
    authenticateHistory(h);
    await h.ctx.loadSessionList();
    h.ctx._touchHistorySession('existing', 'agent-b', { title: 'Updated', last_active: 1800000000 });
    assert.equal(JSON.parse([...h.cacheStorage.values()][0]).sessions[0].title, 'Updated');
    h.run('_historyVisible = false');
    h.ctx._refreshHistoryList();
    assert.equal(h.cacheStorage.size, 0);
    assert.equal(h.run('_historyDirty'), true);
});
