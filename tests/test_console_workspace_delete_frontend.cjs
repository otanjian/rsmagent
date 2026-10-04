// Panel deletion and the recycle bin.
//
// The rules this file pins are the ones a user only discovers by losing
// something, so they are asserted rather than described:
//
//   * a row the server called undeletable stays listed, stays readable, and
//     offers no action at all -- its lock carries the reason, so nothing is
//     offered that would then be declined;
//   * a row's own action is taken before the row's own click, so pressing
//     delete on a folder does not also open it;
//   * the confirmation says what is about to happen (how many items, how much
//     volume, that a directory goes with its contents), because "delete one
//     file" and "delete one folder" are both one click;
//   * the bin is addressed by validated batch name plus index, never by a path,
//     and a restore reports the path the file actually landed on.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { createDocument } = require('./_console_dom.cjs');

const ROOT = path.join(__dirname, '..');
const SOURCE = fs.readFileSync(path.join(ROOT, 'channel/web/static/js/workspace.js'), 'utf8');

const AGENT = 'agent-a';
const AGENT_DIR = `agents/${AGENT}`;

/** Real strings for anything with `{placeholders}`; the key otherwise. */
const CORE_ZH = (() => {
    const ctx = { window: {} };
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(
        path.join(ROOT, 'channel/web/static/js/i18n/core.js'), 'utf8'), ctx);
    return ctx.window.__cowI18N__.core.zh;
})();

function labelFor(key) {
    const value = CORE_ZH[key];
    if (typeof value === 'string' && value.includes('{')) return value;
    return key;
}

// Only controls that act on the panel as a whole are in the toolbar now; a
// row's own actions are on the row, so they are not looked up by id.
const REQUIRED_IDS = [
    'workspace-panel', 'ws-body-files', 'ws-file-list', 'ws-breadcrumb',
    'ws-upload-progress', 'ws-drop-hint',
    'ws-btn-trash', 'ws-btn-refresh', 'ws-btn-trash-back', 'ws-btn-purge-all',
];

const flush = () => new Promise(resolve => setImmediate(resolve));

function setup({ routes = {} } = {}) {
    const document = createDocument();
    const posts = [];
    const toasts = [];
    const confirms = [];

    for (const id of REQUIRED_IDS) {
        const el = document.createElement('div');
        document._root.appendChild(el);
        document.register(id, el);
    }
    const search = document.createElement('div');
    search.className = 'workspace-search';
    document._root.appendChild(search);

    const ctx = {
        currentLang: 'zh',
        t: labelFor,
        escapeHtml: x => String(x == null ? '' : x),
        activeAgentId: AGENT,
        chatAgentCatalog: [{ id: AGENT, visibility: 'private' }],
        localStorage: { getItem: () => null, setItem() {} },
        _wsToast: message => toasts.push(message),
        showConfirmDialog: options => confirms.push(options),
        setTimeout,
        clearTimeout,
        fetch: async (url, init) => {
            posts.push({ url, body: init && init.body ? JSON.parse(init.body) : null });
            const key = Object.keys(routes).find(k => url.includes(k));
            const body = typeof routes[key] === 'function' ? routes[key](url) : (routes[key] || {});
            return { ok: true, status: 200, json: async () => ({ status: 'success', ...body }) };
        },
        window: { addEventListener() {}, removeEventListener() {} },
        document,
    };
    vm.createContext(ctx);
    const initIdx = SOURCE.indexOf('// Init');
    const cut = SOURCE.lastIndexOf('// =====', initIdx);
    assert.ok(cut > 0, 'the Init section is preceded by a banner');
    vm.runInContext(SOURCE.slice(0, cut), ctx);

    return {
        ctx, document, posts, toasts, confirms,
        get: id => document.getElementById(id),
        peek: source => vm.runInContext(source, ctx),
        poke: source => vm.runInContext(source, ctx),
    };
}

/** One row as the listing markup builds it, as a real element. */
function addRow(s, { rel, size = 0, isDir = false, locked = false, lockReason = '' }) {
    const row = s.document.createElement('div');
    row.className = 'ws-file-row';
    row.dataset.wsRel = rel;
    row.dataset.wsSize = String(size);
    row.dataset.wsIsDir = isDir ? '1' : '0';
    if (isDir) row.dataset.wsDir = rel;
    if (locked) {
        row.dataset.wsLocked = '1';
        row.dataset.wsLockReason = lockReason;
    } else {
        // The row's own delete control, as wsRowDeleteHTML renders it.
        const del = s.document.createElement('button');
        del.className = 'ws-row-act ws-row-act-danger';
        del.dataset.wsAct = 'delete';
        row.appendChild(del);
    }
    s.get('ws-file-list').appendChild(row);
    return row;
}

/** One bin row, as `renderTrashEntries` builds it: restore then purge. */
function addBinRow(s, { rel, size = 0, batch = 'batch-1', index = 0, isDir = false }) {
    const row = s.document.createElement('div');
    row.className = 'ws-file-row ws-bin-row';
    row.dataset.wsRel = rel;
    row.dataset.wsSize = String(size);
    row.dataset.wsIsDir = isDir ? '1' : '0';
    row.dataset.wsBinBatch = batch;
    row.dataset.wsBinIndex = String(index);
    for (const [act, cls] of [['restore', 'ws-row-act'], ['purge', 'ws-row-act ws-row-act-danger']]) {
        const btn = s.document.createElement('button');
        btn.className = cls;
        btn.dataset.wsAct = act;
        row.appendChild(btn);
    }
    s.get('ws-file-list').appendChild(row);
    return row;
}

/** The first action button on a row, or null when the row offers none. */
const actOf = (row, cls = '.ws-row-act') => row.querySelector(cls);

// -- refusal codes ---------------------------------------------------------

test('every stable refusal code has a reason in the reader language', () => {
    const s = setup();
    const codes = ['outside_own_directory', 'agent_internal', 'user_container',
        'trash_not_targetable', 'own_directory_root', 'not_agent_workspace',
        'unsafe_user_directory', 'no_user', 'unsafe_path', 'not_found', 'not_movable',
        'not_removable', 'batch_not_found', 'forbidden', 'too_large',
        'too_many_files', 'too_much_total', 'too_deep', 'unreadable', 'network'];
    for (const code of codes) {
        assert.notEqual(s.ctx.wsFailureText(code), code, `${code} has no label`);
    }
    // An unrecognised code is shown as-is rather than as a blank.
    assert.equal(s.ctx.wsFailureText('some_new_code'), 'some_new_code');
});

test('a per-item refusal is summarized by reason, not by repeating each name', () => {
    const s = setup();
    const summary = s.ctx.wsFailureSummary([
        { code: 'outside_own_directory' }, { code: 'outside_own_directory' },
        { code: 'agent_internal' },
    ]);
    assert.match(summary, /ws_lock_outside/);
    assert.match(summary, /×2/);
    assert.match(summary, /ws_lock_agent_internal/);
});

test('the summary stops at three reasons instead of listing the whole batch', () => {
    const s = setup();
    const summary = s.ctx.wsFailureSummary([
        { code: 'a_unknown' }, { code: 'b_unknown' }, { code: 'c_unknown' },
        { code: 'd_unknown' },
    ]);
    assert.equal(summary.split('·').length, 3);
});

// -- which rows offer an action --------------------------------------------

test('a row offers its own delete control only when the server allowed it', () => {
    // The listing is rendered for real here rather than assembled by hand, so
    // the assertion is about what the panel ships.
    const s = setup({ routes: { '/api/workspace/tree': { path: AGENT_DIR, entries: [
        { name: 'notes.txt', path: `${AGENT_DIR}/notes.txt`, kind: 'text',
          is_dir: false, size: 12, deletable: true },
        { name: 'memory', path: `${AGENT_DIR}/memory`, kind: 'directory',
          is_dir: true, size: 0, deletable: false, undeletable_reason: 'agent_internal' },
    ] } } });
    return s.ctx.loadWorkspaceDir(AGENT_DIR).then(() => {
        const html = s.get('ws-file-list').innerHTML;
        assert.equal((html.match(/data-ws-act="delete"/g) || []).length, 1,
            'exactly the deletable row carries a control');
        assert.equal((html.match(/data-ws-locked="1"/g) || []).length, 1,
            'and the other one is marked locked');
        // The rows are ordered as listed, so the control is on the first one.
        const first = html.slice(0, html.indexOf('data-ws-locked'));
        assert.match(first, /notes\.txt/);
    });
});

test('a locked row carries the reason on its lock instead of an action', () => {
    const s = setup({ routes: { '/api/workspace/tree': { path: AGENT_DIR, entries: [
        { name: 'memory', path: `${AGENT_DIR}/memory`, kind: 'directory',
          is_dir: true, size: 0, deletable: false, undeletable_reason: 'agent_internal' },
    ] } } });
    return s.ctx.loadWorkspaceDir(AGENT_DIR).then(() => {
        const html = s.get('ws-file-list').innerHTML;
        assert.ok(!html.includes('data-ws-act="delete"'), 'no action is offered');
        assert.match(html, /ws-row-lock/, 'the lock is shown instead');
        assert.match(html, /title="ws_lock_agent_internal"/,
            'and the reason is reachable from it');
    });
});

test('a bin row offers restore and purge, not a delete', () => {
    const s = setup({ routes: { '/api/workspace/trash': {
        entries: [{ rel: `${AGENT_DIR}/a.txt`, size: 2048, kind: 'text',
                    deleted_at: 1758800000, batch_id: 'b1', index: 0 }],
        total_size: 2048, retention_days: 30,
    } } });
    return s.ctx.loadWorkspaceTrash().then(() => {
        const html = s.get('ws-file-list').innerHTML;
        assert.equal((html.match(/data-ws-act="restore"/g) || []).length, 1);
        assert.equal((html.match(/data-ws-act="purge"/g) || []).length, 1);
        assert.ok(!html.includes('data-ws-act="delete"'),
            'a bin row is not deleted again, it is restored or destroyed');
    });
});

// -- the row's own click ----------------------------------------------------

test('a row action is taken before the row own click, so it does not act too', () => {
    const s = setup({ routes: { '/api/workspace/tree': { path: AGENT_DIR, entries: [] } } });
    // The click listener lives in the init section the harness slices off.
    s.ctx.initWorkspaceFilesTab();
    const dir = addRow(s, { rel: `${AGENT_DIR}/reports`, isDir: true });
    const del = actOf(dir);
    assert.ok(del, 'a deletable directory row offers its own control');
    // The list owns the click listener; firing there with the button as the
    // target is what a real press does.
    s.get('ws-file-list').fire('click', { target: del });
    assert.equal(s.confirms.length, 1, 'the delete confirmation was raised');
    assert.deepEqual(s.posts, [], 'and the folder was not also opened by the same press');

    // Positive control: the row itself still navigates, so the assertion above
    // is about the routing and not about a handler that never ran.
    s.get('ws-file-list').fire('click', { target: dir });
    assert.equal(s.posts.length, 1, 'the row still does its own thing');
});

test('a bin row has nothing to preview, so its own click is inert', () => {
    const s = setup();
    s.ctx.initWorkspaceFilesTab();
    s.poke('wsTrashMode = true;');
    const row = addBinRow(s, { rel: `${AGENT_DIR}/a.txt`, batch: 'b1', index: 0 });
    s.get('ws-file-list').fire('click', { target: row });
    assert.equal(row.classList.contains('active'), false);
    assert.deepEqual(s.posts, []);
});

// -- delete ----------------------------------------------------------------

test('the confirmation says how much, and that it is recoverable', () => {
    const s = setup();
    const row = addRow(s, { rel: `${AGENT_DIR}/a.txt`, size: 1024 * 1024 });
    s.ctx.askWorkspaceDelete(row);
    assert.equal(s.confirms.length, 1);
    const { title, message, okText } = s.confirms[0];
    // `title`/`okText` carry no placeholder, so the harness leaves them as keys
    // (their resolution is `test_console_i18n_coverage.cjs`'s job); the message
    // carries the counts the user is agreeing to, so it is asserted for real.
    assert.equal(title, 'ws_delete_confirm_title');
    assert.match(message, /将移入回收站 1 项 · 共 1\.0MB/);
    assert.match(message, /ws_delete_confirm_trash/);
    assert.equal(okText, 'ws_delete_go');
});

test('a folder confirmation says the contents go with it', () => {
    const s = setup();
    const row = addRow(s, { rel: `${AGENT_DIR}/reports`, isDir: true });
    s.ctx.askWorkspaceDelete(row);
    assert.match(s.confirms[0].message,
        /其中 1 个文件夹会连同其全部内容一起移入/);
});

test('confirming posts that one relative path, and nothing else', async () => {
    const s = setup({ routes: { '/api/workspace/delete': { deleted: ['x'] },
                                '/api/workspace/tree': { path: AGENT_DIR, entries: [] } } });
    const row = addRow(s, { rel: `${AGENT_DIR}/a.txt` });
    addRow(s, { rel: `${AGENT_DIR}/b.txt` });
    s.poke(`wsCurrentDir = ${JSON.stringify(AGENT_DIR)};`);
    s.ctx.askWorkspaceDelete(row);
    await s.confirms[0].onConfirm();
    const post = s.posts.find(p => p.url.includes('/api/workspace/delete'));
    assert.deepEqual(post.body, { targets: [`${AGENT_DIR}/a.txt`] },
        'a row action addresses its own row, not a selection');
    assert.deepEqual(s.toasts, ['已移入回收站 1 项 · 打开回收站']);
});

test('a locked row cannot be deleted even if the call is made', async () => {
    const s = setup({ routes: { '/api/workspace/delete': {}, '/api/workspace/tree': {} } });
    const locked = addRow(s, { rel: `${AGENT_DIR}/memory`, isDir: true,
        locked: true, lockReason: 'agent_internal' });
    s.ctx.askWorkspaceDelete(locked);
    assert.deepEqual(s.confirms, [], 'no confirmation is raised for a locked row');
    assert.deepEqual(s.posts, [], 'and nothing is sent');
});

test('a refusal to delete is reported, and the listing is re-read', async () => {
    const s = setup({ routes: {
        '/api/workspace/delete': { deleted: [], failed: [{ rel: 'x', code: 'not_removable' }] },
        '/api/workspace/tree': { path: AGENT_DIR, entries: [] },
    } });
    const row = addRow(s, { rel: `${AGENT_DIR}/a.txt` });
    s.poke(`wsCurrentDir = ${JSON.stringify(AGENT_DIR)};`);
    s.ctx.askWorkspaceDelete(row);
    await s.confirms[0].onConfirm();
    assert.match(s.toasts[0], /ws_delete_missing/);
});

// -- toolbar modes ---------------------------------------------------------

test('the toolbar shows the bin controls only in the bin', () => {
    const s = setup();
    s.ctx.wsUpdateToolbarState();
    const shown = id => !s.get(id).classList.contains('hidden');
    assert.equal(shown('ws-btn-trash'), true);
    assert.equal(shown('ws-btn-refresh'), true);
    assert.equal(shown('ws-btn-trash-back'), false);
    assert.equal(shown('ws-btn-purge-all'), false);

    s.poke('wsTrashMode = true; wsUpdateToolbarState();');
    assert.equal(shown('ws-btn-trash'), false);
    assert.equal(shown('ws-btn-refresh'), false);
    assert.equal(shown('ws-btn-trash-back'), true);
    assert.equal(shown('ws-btn-purge-all'), true);
    // The search box searches the workspace; it has nothing to search in a bin.
    assert.equal(s.document.querySelector('.workspace-search').classList.contains('hidden'), true);
});

test('browsing a directory leaves the bin mode behind', async () => {
    const s = setup({ routes: { '/api/workspace/tree': { path: AGENT_DIR, entries: [] } } });
    s.poke('wsTrashMode = true; wsUpdateToolbarState();');
    await s.ctx.loadWorkspaceDir(AGENT_DIR);
    assert.equal(s.peek('wsTrashMode'), false);
    assert.equal(s.get('ws-btn-trash').classList.contains('hidden'), false);
});

// -- the bin listing -------------------------------------------------------

test('the bin header says how much it holds and how long it keeps it', async () => {
    const s = setup({ routes: { '/api/workspace/trash': {
        entries: [{ rel: `${AGENT_DIR}/a.txt`, size: 2048, kind: 'text',
                    deleted_at: 1758800000, batch_id: 'b1', index: 0 }],
        total_size: 2048, retention_days: 30,
    } } });
    await s.ctx.loadWorkspaceTrash();
    const header = s.get('ws-breadcrumb').innerHTML;
    assert.match(header, /ws_trash_title/);
    assert.match(header, /2\.0KB/);
    assert.match(header, /保留 30 天/);
    const list = s.get('ws-file-list').innerHTML;
    assert.match(list, /data-ws-bin-batch="b1"/);
    assert.match(list, /data-ws-bin-index="0"/);
    assert.match(list, /删除于/);
});

test('an empty bin says it is empty instead of showing nothing', async () => {
    const s = setup({ routes: { '/api/workspace/trash': { entries: [], total_size: 0,
        retention_days: 30 } } });
    await s.ctx.loadWorkspaceTrash();
    assert.match(s.get('ws-file-list').innerHTML, /ws_trash_empty_state/);
});

test('a refusal to list the bin is shown in the reader language', async () => {
    const s = setup();
    s.ctx.fetch = async () => ({ ok: false, status: 403,
        json: async () => ({ status: 'error', code: 'forbidden', message: 'forbidden' }) });
    await s.ctx.loadWorkspaceTrash();
    const html = s.get('ws-file-list').innerHTML;
    assert.match(html, /<span>ws_forbidden<\/span>/);
    assert.ok(!html.includes('>forbidden<'), 'the raw reason is not shown');
});

test('a bin listing from a previous scope does not paint the new one', async () => {
    const s = setup({ routes: { '/api/workspace/trash': { entries: [{ rel: 'stale.txt',
        size: 1, kind: 'text', deleted_at: 0, batch_id: 'b1', index: 0 }] } } });
    let release;
    const held = new Promise(resolve => { release = resolve; });
    const inner = s.ctx.fetch;
    s.ctx.fetch = async (url, init) => { await held; return inner(url, init); };
    const pending = s.ctx.loadWorkspaceTrash();
    // Another Agent (or session) becomes the scope while the read is in flight.
    s.poke('wsNewScopeEpoch();');
    release();
    await pending;
    assert.ok(!s.get('ws-file-list').innerHTML.includes('stale.txt'),
        'the bin of the scope the user left was rendered');
});

// -- restore / purge -------------------------------------------------------

test('a restore addresses a batch by name and index, never by path', async () => {
    const s = setup({ routes: {
        '/api/workspace/trash/restore': { restored: [
            { rel: `${AGENT_DIR}/a.txt`, path: `${AGENT_DIR}/a.txt` }] },
        '/api/workspace/trash': { entries: [], total_size: 0, retention_days: 30 },
    } });
    const row = addBinRow(s, { rel: `${AGENT_DIR}/a.txt`, batch: 'batch-7', index: 3 });
    await s.ctx.restoreTrashRow(row);
    const post = s.posts.find(p => p.url.includes('/api/workspace/trash/restore'));
    assert.deepEqual(post.body, { batch_id: 'batch-7', indices: [3] });
    assert.deepEqual(s.toasts, ['已恢复 1 项']);
});

test('a restore onto a taken name reports the path it actually landed on', () => {
    const s = setup();
    const summary = s.ctx.wsRestoreSummary(
        [{ rel: 'a.txt', path: `${AGENT_DIR}/a (1).txt`, renamed: true },
         { rel: 'b.txt', path: `${AGENT_DIR}/b.txt` }], []);
    assert.match(summary, /已恢复 2 项/);
    assert.match(summary, /agents\/agent-a\/a \(1\)\.txt/);
});

test('a refused restore keeps its reason and still refreshes the bin', async () => {
    const s = setup({ routes: {
        '/api/workspace/trash/restore': { restored: [], failed: [
            { rel: 'x', code: 'outside_own_directory' }] },
        '/api/workspace/trash': { entries: [], total_size: 0, retention_days: 30 },
    } });
    const row = addBinRow(s, { rel: 'x', batch: 'b1', index: 0 });
    await s.ctx.restoreTrashRow(row);
    assert.match(s.toasts[0], /ws_lock_outside/);
    assert.equal(s.posts.filter(p => p.url.includes('/api/workspace/trash?')
        || p.url.endsWith('/api/workspace/trash')).length, 1);
});

test('purging names the count and says it cannot be undone', () => {
    const s = setup();
    const row = addBinRow(s, { rel: 'x', batch: 'b1', index: 0 });
    s.ctx.askWorkspacePurge(row);
    const { title, message, okText } = s.confirms[0];
    assert.equal(title, 'ws_trash_purge_confirm_title');
    assert.match(message, /这 1 项/);
    assert.match(message, /无法恢复/);
    assert.equal(okText, 'ws_trash_purge');
});

test('purging one row sends that row address, batch and index', async () => {
    const s = setup({ routes: {
        '/api/workspace/trash/purge': { purged: ['x'] },
        '/api/workspace/trash': { entries: [], total_size: 0, retention_days: 30 },
    } });
    const row = addBinRow(s, { rel: 'x', batch: 'b1', index: 2 });
    s.ctx.askWorkspacePurge(row);
    await s.confirms[0].onConfirm();
    const posts = s.posts.filter(p => p.url.includes('/api/workspace/trash/purge'));
    assert.deepEqual(posts.map(p => p.body), [{ batch_id: 'b1', indices: [2] }]);
    assert.deepEqual(s.toasts, ['已彻底删除 1 项']);
});

test('a row with no batch address is not acted on', async () => {
    const s = setup({ routes: { '/api/workspace/trash': { entries: [] } } });
    const row = s.document.createElement('div');
    row.className = 'ws-file-row ws-bin-row';
    row.dataset.wsRel = `${AGENT_DIR}/a.txt`;
    s.get('ws-file-list').appendChild(row);
    s.ctx.askWorkspacePurge(row);
    await s.ctx.restoreTrashRow(row);
    assert.deepEqual(s.confirms, []);
    assert.deepEqual(s.posts, [], 'an unaddressable row is ignored rather than guessed at');
});

test('emptying the bin is one request with no batch, and it says how many', async () => {
    const s = setup({ routes: {
        '/api/workspace/trash/purge': { purged: ['x', 'y', 'z'] },
        '/api/workspace/trash': { entries: [], total_size: 0, retention_days: 30 },
    } });
    s.poke(`wsTrashEntries = ${JSON.stringify([
        { rel: 'x', batch_id: 'b1', index: 0 }, { rel: 'y', batch_id: 'b1', index: 1 },
        { rel: 'z', batch_id: 'b2', index: 0 }])};`);
    s.ctx.emptyWorkspaceTrash();
    assert.match(s.confirms[0].message, /回收站中的 3 项/);
    await s.confirms[0].onConfirm();
    const posts = s.posts.filter(p => p.url.includes('/api/workspace/trash/purge'));
    assert.equal(posts.length, 1);
    assert.ok(!posts[0].body || posts[0].body.batch_id === undefined,
        'an empty bin request carries no batch');
    assert.deepEqual(s.toasts, ['已彻底删除 3 项']);
});

test('an empty bin is not asked to empty itself again', () => {
    const s = setup();
    s.poke('wsTrashEntries = [];');
    s.ctx.emptyWorkspaceTrash();
    assert.deepEqual(s.confirms, []);
    assert.deepEqual(s.posts, []);
});

test('a failed purge is reported, and the bin is re-read either way', async () => {
    const s = setup({ routes: {
        '/api/workspace/trash/purge': { purged: [], failed: [{ rel: 'x', code: 'not_removable' }] },
        '/api/workspace/trash': { entries: [], total_size: 0, retention_days: 30 },
    } });
    const row = addBinRow(s, { rel: 'x', batch: 'b1', index: 0 });
    s.ctx.askWorkspacePurge(row);
    await s.confirms[0].onConfirm();
    await flush();
    assert.match(s.toasts[0], /ws_delete_missing/);
    // `wsScopedPath` appends the scope, so the read is matched by prefix.
    const reads = s.posts.filter(p => p.url.includes('/api/workspace/trash?'));
    assert.equal(reads.length, 1, 'the bin is re-read so a refused entry stays visible');
});
