// Run the shipped sidebar-history rename handler against a small DOM and
// controlled transport. Backend tests own the title write; these cases pin the
// sidebar affordance: double-click/F2 opens an editor seeded with the current
// title without opening the session, Enter/blur save, Escape and blank edits do
// not write, and a failed write rolls the title back with a reason.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
const start = source.indexOf('// === SIDEBAR_RECENT_BEGIN ===');
const end = source.indexOf('// Never run sidebar init inline', start);
assert.ok(start >= 0 && end > start, 'Missing sidebar recent section in console.js');
const sidebarSource = source.slice(start, end);

function element(tagName = 'div') {
    const classes = new Set();
    let text = '', html = '';
    const el = {
        tagName: tagName.toUpperCase(), value: '', dataset: {}, attrs: {}, handlers: {},
        children: [], parentNode: null, disabled: false, hidden: false, type: '',
        get firstChild() { return this.children[0] || null; },
        get textContent() { return text; },
        set textContent(value) { text = String(value); html = ''; this.children = []; },
        get innerHTML() { return html; },
        set innerHTML(value) { html = String(value); text = ''; this.children = []; },
        get className() { return [...classes].join(' '); },
        set className(value) { classes.clear(); String(value).split(/\s+/).filter(Boolean).forEach(v => classes.add(v)); },
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
        getAttribute(key) { return this.attrs[key] ?? null; },
        removeAttribute(key) { delete this.attrs[key]; },
        addEventListener(name, fn) { this.handlers[name] = fn; },
        appendChild(child) {
            if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(el => el !== child);
            this.children.push(child); child.parentNode = this; return child;
        },
        insertBefore(child, ref) {
            if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(el => el !== child);
            const idx = ref ? this.children.indexOf(ref) : -1;
            if (idx < 0) this.children.push(child); else this.children.splice(idx, 0, child);
            child.parentNode = this; return child;
        },
        removeChild(child) {
            this.children = this.children.filter(el => el !== child); child.parentNode = null; return child;
        },
        remove() { if (this.parentNode) this.parentNode.removeChild(this); },
        focus() { this.focused = true; },
        select() { this.selected = true; },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
        querySelectorAll(selector) {
            const matches = child => selector.startsWith('.')
                ? child.className.split(' ').includes(selector.slice(1))
                : child.tagName.toLowerCase() === selector.toLowerCase();
            return this.children.flatMap(child => [
                ...(matches(child) ? [child] : []), ...child.querySelectorAll(selector),
            ]);
        },
    };
    return el;
}

async function settle() {
    for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve));
}

function session(id, extra = {}) {
    return { session_id: id, title: id, last_active: 1700000000, pinned: 0,
        agent: { id: 'agent-a', name: 'Agent A' }, project: null, ...extra };
}
function payload(sessions, extra = {}) {
    return { ok: true, status: 200, json: async () => ({ status: 'success', sessions,
        total: sessions.length, has_more: false, group_mode: 'time', project_order: [], ...extra }) };
}

function setup(fetchImpl) {
    const nodes = new Map();
    const node = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
    const calls = [];
    const toasts = [];
    const switched = [];
    const ctx = vm.createContext({
        console, Date, Intl, URLSearchParams, AbortController,
        activeAgentId: 'agent-a', sessionId: 'current', _authEpoch: 1,
        sessionStorage: { getItem: () => 'tenant-one' }, _accountAppVisible: true,
        location: { pathname: '/chat' },
        _navAreaFromPath: () => 'workbench',
        window: { innerWidth: 1440 },
        document: {
            getElementById: node, createElement: element,
            querySelector: () => null, querySelectorAll: () => [],
        },
        localStorage: { getItem: () => null, setItem() {} },
        t: key => key, escapeHtml: value => String(value),
        switchSession: (sid, aid) => switched.push([sid, aid]),
        _wsToast: msg => toasts.push(msg),
        requestAnimationFrame: fn => fn(),
        setTimeout: (fn) => { fn(); return 0; }, clearTimeout() {},
        fetch(url, options) {
            calls.push({ url, options });
            return Promise.resolve(fetchImpl ? fetchImpl(url, options) : payload([]));
        },
    });
    const run = code => vm.runInContext(code, ctx);
    run(sidebarSource);
    // ``_sidebarRecentItems`` is a script-level ``let``, so assigning the global
    // object property from Node would not be visible to the shipped code.
    const setItems = items => run(`_sidebarRecentItems = ${JSON.stringify(items)};`);
    const titles = () => run('_sidebarRecentItems.map(s => s.title)');
    const rowFor = id => node('sidebar-recent-list')
        .querySelectorAll('.sidebar-recent-row').find(el => el.dataset.sessionId === id);
    const editorFor = id => rowFor(id).querySelectorAll('.sidebar-recent-rename-input')[0] || null;
    const mainFor = id => rowFor(id).querySelectorAll('.sidebar-recent-item')[0] || null;
    const btnFor = (id, cls) => rowFor(id).querySelectorAll('.' + cls)[0] || null;
    return { ctx, run, node, calls, toasts, switched, setItems, titles, rowFor, editorFor, mainFor, btnFor };
}

function startEdit(h, id, via = 'dblclick') {
    const target = h.mainFor(id);
    assert.ok(target, `${id} has a main button`);
    if (via === 'dblclick') target.handlers.dblclick({ preventDefault() {}, stopPropagation() {} });
    else target.handlers.keydown({ key: 'F2', preventDefault() {}, stopPropagation() {} });
}

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

test('a history read arriving after editing starts preserves the input and refreshes after Escape', async () => {
    const pending = deferred();
    let reads = 0;
    const h = withOne(session('s1', { title: 'Old title' }), () => ++reads === 1
        ? pending.promise : payload([session('s1', { title: 'Latest title' })]));
    h.ctx.loadSidebarRecentSessions();
    startEdit(h, 's1'); const input = h.editorFor('s1'); input.value = 'Keep my draft';
    pending.resolve(payload([session('s1', { title: 'Stale title' })])); await settle();
    assert.equal(h.editorFor('s1'), input); assert.equal(input.value, 'Keep my draft');
    input.handlers.keydown({ key: 'Escape', preventDefault() {} }); await settle();
    assert.equal(h.calls.length, 2); assert.equal(h.mainFor('s1').title, 'Latest title');
    assert.equal(h.switched.length, 0);
});

test('refresh during an edit waits until the title PUT finishes, then reads once', async () => {
    const write = deferred();
    const h = withOne(session('s1', { title: 'Old title' }), (url, options) => options?.method === 'PUT'
        ? write.promise : payload([session('s1', { title: 'Saved title' })]));
    startEdit(h, 's1'); const input = h.editorFor('s1'); input.value = 'Saved title';
    h.ctx.loadSidebarRecentSessions(); h.ctx.loadSidebarRecentSessions();
    assert.equal(h.calls.length, 0); assert.equal(h.editorFor('s1'), input);
    input.handlers.keydown({ key: 'Enter', preventDefault() {} }); await settle();
    assert.equal(h.calls.length, 1); assert.equal(h.mainFor('s1').title, 'Saved title');
    write.resolve(payload([])); await settle();
    assert.equal(h.calls.length, 2); assert.equal(h.mainFor('s1').title, 'Saved title');
});

test('a late failed read cannot discard a rename input', async () => {
    for (const network of [false, true]) {
        const pending = deferred(), h = withOne(session('s1'), () => pending.promise);
        h.ctx.loadSidebarRecentSessions(); startEdit(h, 's1');
        const input = h.editorFor('s1'); input.value = 'Draft';
        if (network) pending.reject(Error('Offline'));
        else pending.resolve({ ok: false, json: async () => ({ status: 'error' }) });
        await settle(); assert.equal(h.editorFor('s1'), input); assert.equal(input.value, 'Draft');
    }
});

test('sidebar accepts only the latest read in the original identity and tenant', async () => {
    const first = deferred(), last = deferred(); let reads = 0;
    const h = withOne(session('s1'), () => ++reads === 1 ? first.promise : last.promise);
    h.ctx.loadSidebarRecentSessions(); h.ctx.loadSidebarRecentSessions();
    last.resolve(payload([session('latest')])); await settle();
    first.resolve(payload([session('stale')])); await settle();
    assert.equal(h.rowFor('stale'), undefined); assert.ok(h.rowFor('latest'));
    for (const change of [h => h.ctx._authEpoch++, h => h.ctx.sessionStorage.getItem = () => 'tenant-two']) {
        const pending = deferred(), h = withOne(session('s1'), () => pending.promise);
        h.ctx.loadSidebarRecentSessions(); change(h);
        pending.resolve(payload([session('other-user')])); await settle();
        assert.ok(h.rowFor('s1')); assert.equal(h.rowFor('other-user'), undefined);
    }
});

test('reopening recent history reads again, closing does not; hidden grants never trigger reads', async () => {
    const h = setup(); h.ctx._initSidebarRecent(); await settle();
    const click = () => h.node('sidebar-recent-toggle').handlers.click({ preventDefault() {}, stopPropagation() {} });
    assert.equal(h.calls.length, 1); click(); await settle(); assert.equal(h.calls.length, 2);
    click(); await settle(); assert.equal(h.calls.length, 2);
    click(); await settle(); assert.equal(h.calls.length, 3);
    h.ctx._viewNavDenied = () => true; h.ctx.loadSidebarRecentSessions();
    assert.equal(h.calls.length, 3); assert.equal(h.node('sidebar-recent').classList.contains('hidden'), true);
});

test('both sidebar layouts retain their existing preview limits', async () => {
    for (const refined of [false, true]) {
        const h = setup(() => payload(Array.from({ length: 15 }, (_, i) => session('s' + i))));
        h.ctx.sidebarLaunchV2 = () => refined; await h.ctx.loadSidebarRecentSessions();
        assert.equal(h.node('sidebar-recent-list').children.length, refined ? 5 : 10);
        assert.match(h.calls[0].url, new RegExp('page_size=' + (refined ? 5 : 10)));
    }
});

function withOne(session_, fetchImpl) {
    const h = setup(fetchImpl);
    h.setItems([session_]);
    h.run('renderSidebarRecentSessions()');
    return h;
}

test('every sidebar row shows a rename button beside the archive button', () => {
    const h = withOne(session('s1', { title: 'Alpha' }));

    const renameBtn = h.btnFor('s1', 'sidebar-recent-rename-btn');
    const archiveBtn = h.btnFor('s1', 'sidebar-recent-archive-btn');
    assert.ok(renameBtn, 'the row exposes a rename button');
    assert.ok(archiveBtn, 'the archive button is still there');
    assert.equal(renameBtn.getAttribute('aria-label'), 'rename_session: Alpha');
    assert.equal(renameBtn.title, 'rename_session');
    const controls = h.rowFor('s1').querySelectorAll('button')
        .filter(el => el.className.includes('sidebar-recent-rename-btn')
            || el.className.includes('sidebar-recent-archive-btn'));
    assert.deepEqual(controls.map(el => el.className.includes('sidebar-recent-rename-btn')),
        [true, false], 'rename comes before archive');
});

test('the rename and archive controls share one CSS rule set', () => {
    const css = fs.readFileSync(path.join(__dirname, '../channel/web/static/css/console.css'), 'utf8');
    const ARCHIVE = 'sidebar-recent-archive-btn';
    const RENAME = 'sidebar-recent-rename-btn';
    // Every selector list that styles one of the two controls must style both:
    // same box, same hover feedback, same reveal rule. This is the guard against
    // the two buttons drifting apart.
    const selectorLists = [...css.matchAll(/([^{}]+)\{/g)]
        .map(m => m[1])
        .filter(sel => sel.includes(ARCHIVE) || sel.includes(RENAME));
    assert.ok(selectorLists.length >= 3, 'both controls are styled by several rules');
    for (const sel of selectorLists) {
        assert.ok(sel.includes(ARCHIVE) && sel.includes(RENAME),
            `selector list must pair both controls, got: ${sel.trim().split('\n').map(s => s.trim()).join(' | ')}`);
    }
    // Reveal and hover must be symmetric per state, not merely present somewhere
    // in the list: dropping one state for one control is exactly the drift this
    // guards against.
    for (const state of ['.sidebar-recent-row:hover', '.sidebar-recent-row:focus-within']) {
        for (const cls of [ARCHIVE, RENAME]) {
            assert.ok(css.includes(`${state} .${cls}`),
                `${state} must reveal ${cls} just like its sibling control`);
        }
    }
});

test('the rename button opens the inline editor without opening or archiving the session', async () => {
    const h = withOne(session('s1', { title: 'Alpha' }));

    h.btnFor('s1', 'sidebar-recent-rename-btn').handlers.click({ stopPropagation() {} });

    const editor = h.editorFor('s1');
    assert.ok(editor, 'the rename button opens the editor');
    assert.equal(editor.value, 'Alpha');
    assert.equal(editor.selected, true);
    assert.deepEqual(h.switched, [], 'the rename button must not open the session');
    assert.equal(h.calls.length, 0, 'the rename button must not archive or write anything');
});

test('the rename button and double-click share the same save path', async () => {
    const h = withOne(session('s1', { title: 'Alpha' }), () => payload([]));

    h.btnFor('s1', 'sidebar-recent-rename-btn').handlers.click({ stopPropagation() {} });
    const editor = h.editorFor('s1');
    editor.value = 'From button';
    editor.handlers.keydown({ key: 'Enter', isComposing: false, keyCode: 13, preventDefault() {} });
    await settle();

    const put = h.calls.find(c => c.options && c.options.method === 'PUT');
    assert.ok(put, 'the button-opened editor saves through the same PUT');
    assert.match(put.options.body, /"title":"From button"/);
    assert.deepEqual([...h.titles()], ['From button']);
    assert.deepEqual(h.switched, []);
});

test('double-clicking a sidebar title opens an inline editor seeded with the title without opening the session', () => {
    const h = withOne(session('s1', { title: 'Alpha' }));

    startEdit(h, 's1');

    const editor = h.editorFor('s1');
    assert.ok(editor, 'an inline editor appears on double-click');
    assert.equal(editor.value, 'Alpha');
    assert.equal(editor.focused, true, 'the editor takes focus');
    assert.equal(editor.selected, true, 'the title is selected for replacement');
    assert.equal(h.mainFor('s1').classList.contains('hidden'), true,
        'the open button is hidden while editing');
    assert.deepEqual(h.switched, [], 'starting an edit must not open the session');
    assert.equal(h.calls.length, 0, 'no request is issued until the edit is saved');
});

test('F2 on a focused row opens the same inline editor', () => {
    const h = withOne(session('s1', { title: 'Alpha' }));

    startEdit(h, 's1', 'f2');

    assert.ok(h.editorFor('s1'), 'F2 provides a keyboard entry point');
    assert.deepEqual(h.switched, []);
});

test('Enter saves the title through PUT, updates the row silently and closes the editor', async () => {
    const h = withOne(session('s1', { title: 'Alpha' }), () => payload([]));
    startEdit(h, 's1');
    const editor = h.editorFor('s1');
    editor.value = 'Alpha renamed';

    editor.handlers.keydown({ key: 'Enter', isComposing: false, keyCode: 13, preventDefault() {} });
    await settle();

    const put = h.calls.find(c => c.options && c.options.method === 'PUT');
    assert.ok(put, 'the rename writes through PUT');
    assert.equal(put.url, '/api/sessions/s1?agent_id=agent-a');
    assert.match(put.options.body, /"title":"Alpha renamed"/);
    assert.deepEqual([...h.titles()], ['Alpha renamed'], 'the cached title is updated');
    assert.equal(h.editorFor('s1'), null, 'the editor closes after saving');
    assert.equal(h.mainFor('s1').classList.contains('hidden'), false, 'the open button returns');
    assert.equal(h.mainFor('s1').textContent, 'Alpha renamed', 'the row shows the new title');
    assert.deepEqual(h.toasts, [], 'a successful rename stays silent');
});

test('Escape cancels the edit without writing', async () => {
    const h = withOne(session('s1', { title: 'Alpha' }));
    startEdit(h, 's1');
    const editor = h.editorFor('s1');
    editor.value = 'Discarded';

    editor.handlers.keydown({ key: 'Escape', preventDefault() {} });
    await settle();

    assert.equal(h.calls.length, 0, 'Escape issues no request');
    assert.equal(h.editorFor('s1'), null, 'the editor closes');
    assert.equal(h.mainFor('s1').textContent, 'Alpha', 'the original title is restored');
    assert.deepEqual([...h.titles()], ['Alpha']);
});

test('blur saves a changed title and skips an unchanged one', async () => {
    const h = withOne(session('s1', { title: 'Alpha' }), () => payload([]));

    startEdit(h, 's1');
    h.editorFor('s1').handlers.blur();
    await settle();
    assert.equal(h.calls.length, 0, 'blur without a change issues no request');

    startEdit(h, 's1');
    const editor = h.editorFor('s1');
    editor.value = 'Beta';
    editor.handlers.blur();
    await settle();

    const put = h.calls.find(c => c.options && c.options.method === 'PUT');
    assert.ok(put, 'blur saves the changed title');
    assert.match(put.options.body, /"title":"Beta"/);
});

test('a blank title is never written', async () => {
    const h = withOne(session('s1', { title: 'Alpha' }));
    startEdit(h, 's1');
    const editor = h.editorFor('s1');
    editor.value = '   ';

    editor.handlers.keydown({ key: 'Enter', isComposing: false, keyCode: 13, preventDefault() {} });
    await settle();

    assert.equal(h.calls.length, 0, 'a blank title issues no request');
    assert.equal(h.mainFor('s1').textContent, 'Alpha', 'the original title stays');
    assert.deepEqual([...h.titles()], ['Alpha']);
});

test('a failed rename rolls the title back and reports the reason', async () => {
    const h = withOne(session('s1', { title: 'Alpha' }), () => ({
        ok: false, status: 500,
        json: async () => ({ status: 'error', message: 'rename boom' }),
    }));
    startEdit(h, 's1');
    const editor = h.editorFor('s1');
    editor.value = 'Beta';

    editor.handlers.keydown({ key: 'Enter', isComposing: false, keyCode: 13, preventDefault() {} });
    await settle();

    assert.equal(h.mainFor('s1').textContent, 'Alpha', 'the row falls back to the old title');
    assert.deepEqual([...h.titles()], ['Alpha'], 'the cached title is rolled back too');
    assert.ok(h.toasts.includes('rename boom'), 'the failure reason is surfaced');
});

test('editing one row never opens the session and leaves other rows alone', async () => {
    const h = setup(() => payload([]));
    h.setItems([session('s1', { title: 'Alpha' }), session('s2', { title: 'Gamma' })]);
    h.run('renderSidebarRecentSessions()');

    startEdit(h, 's1');
    const editor = h.editorFor('s1');
    editor.handlers.click({ stopPropagation() {} });
    editor.handlers.mousedown({ stopPropagation() {} });
    editor.value = 'Alpha 2';
    editor.handlers.keydown({ key: 'Enter', isComposing: false, keyCode: 13, preventDefault() {} });
    await settle();

    assert.deepEqual(h.switched, [], 'an edit never opens a session');
    assert.deepEqual([...h.titles()], ['Alpha 2', 'Gamma'], 'only the edited row changes');
    assert.equal(h.mainFor('s2').textContent, 'Gamma');
    assert.equal(h.editorFor('s2'), null, 'no editor appears on other rows');
});
