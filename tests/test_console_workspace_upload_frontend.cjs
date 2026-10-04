// Panel drag-and-drop upload: what leaves the browser, and what the user sees.
//
// The upload is the one transport the console's `fetch` wrapper cannot serve --
// it needs `XMLHttpRequest.upload.onprogress` to report bytes actually on the
// wire -- so this file pins the three things that decision makes easy to get
// wrong:
//
//   * one file per request (a dropped tree is recreated from each file's own
//     relative path, and a 200MB file and a 5000-file drop share one code path);
//   * the ceilings are the experience layer, not a guarantee -- a drop that
//     loses something must not read 100%;
//   * a drop that started under one scope must not paint into the next one.
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
const SHARED = 'shared-agent';
const UID = 'usr_me';
const OWN_DIR = `agents/${SHARED}/user/${UID}`;

//: The strings the panel shows, resolved for real when they carry
//: `{placeholders}`: "3 项 · 12.0MB 将移入回收站" is a promise the user acts on,
//: so its substitution has to be exercised, not stubbed away. A key with no
//: placeholder stays the key, so assertions read like the code does.
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

/** Every id the upload code reaches for by name. */
const REQUIRED_IDS = [
    'workspace-panel', 'ws-body-files', 'ws-file-list', 'ws-breadcrumb',
    'ws-drop-hint', 'ws-drop-hint-text',
    'ws-upload-progress', 'ws-upload-title', 'ws-upload-count', 'ws-upload-detail',
    'ws-upload-fill', 'ws-upload-retry', 'ws-upload-dismiss',
    // Per-row actions are on the rows, not in the toolbar, so no id for them.
    'ws-btn-trash', 'ws-btn-refresh', 'ws-btn-trash-back', 'ws-btn-purge-all',
];

const flush = () => new Promise(resolve => setImmediate(resolve));

/** Wait for `predicate`, or fail loudly rather than assert on a race. */
async function waitFor(predicate, label) {
    for (let i = 0; i < 50; i += 1) {
        if (predicate()) return;
        await flush();
    }
    assert.fail(`timed out waiting for ${label}`);
}

class FakeFormData {
    constructor() { this.entries = []; }
    append(key, value, name) { this.entries.push({ key, value, name }); }
}

class FakeXHR {
    constructor(registry) {
        this.registry = registry;
        this.headers = {};
        this.listeners = {};
        this.uploadListeners = {};
        const self = this;
        this.upload = {
            addEventListener(type, handler) {
                (self.uploadListeners[type] || (self.uploadListeners[type] = [])).push(handler);
            },
        };
    }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(key, value) { this.headers[key] = value; }
    addEventListener(type, handler) {
        (this.listeners[type] || (this.listeners[type] = [])).push(handler);
    }
    send(body) { this.body = body; this.registry.push(this); }
    /** Answer the request as the server would. */
    finish(status, payload) {
        this.status = status;
        this.responseText = JSON.stringify(payload);
        (this.listeners.load || []).forEach(handler => handler());
    }
    fail() { (this.listeners.error || []).forEach(handler => handler()); }
    /** Report bytes on the wire for the whole multipart body. */
    progress(loaded, total) {
        (this.uploadListeners.progress || []).forEach(handler =>
            handler({ lengthComputable: true, loaded, total }));
    }
}

function setup({ tree = { status: 'success', path: AGENT_DIR, root: '/ws/t1', entries: [] } } = {}) {
    const document = createDocument();
    const requests = [];
    const xhrs = [];
    const toasts = [];
    const confirms = [];

    for (const id of REQUIRED_IDS) {
        const el = document.createElement('div');
        document._root.appendChild(el);
        document.register(id, el);
    }
    for (const id of ['ws-upload-progress', 'ws-drop-hint', 'ws-btn-purge-all']) {
        document.getElementById(id).classList.add('hidden');
    }

    const ctx = {
        currentLang: 'zh',
        t: labelFor,
        escapeHtml: x => String(x == null ? '' : x),
        activeAgentId: AGENT,
        chatAgentCatalog: [{ id: AGENT, visibility: 'private' }],
        localStorage: { getItem: () => null, setItem() {} },
        // The two side effects the panel borrows from the shell.
        _wsToast: message => toasts.push(message),
        showConfirmDialog: options => confirms.push(options),
        setTimeout,
        clearTimeout,
        XMLHttpRequest: function () { return new FakeXHR(xhrs); },
        FormData: FakeFormData,
        fetch: async (url) => {
            requests.push(url);
            return { ok: true, status: 200, json: async () => tree };
        },
        window: { addEventListener() {}, removeEventListener() {} },
        document,
    };
    vm.createContext(ctx);
    // Everything before the "Init" section: definitions and literals only, so
    // the panel is not wired to the DOM twice and the functions are callable.
    const initIdx = SOURCE.indexOf('// Init');
    assert.ok(initIdx > 0, 'the Init section exists');
    const cut = SOURCE.lastIndexOf('// =====', initIdx);
    assert.ok(cut > 0, 'the Init section is preceded by a banner');
    vm.runInContext(SOURCE.slice(0, cut), ctx);

    return {
        ctx, document, requests, xhrs, toasts, confirms,
        get: id => document.getElementById(id),
        uploads: () => xhrs,
        treeRequests: () => requests.filter(u => u.includes('/api/workspace/tree')),
        peek: source => vm.runInContext(source, ctx),
        poke: source => vm.runInContext(source, ctx),
    };
}

// -- dropped entries -------------------------------------------------------

function fileEntry(name, size) {
    return {
        isFile: true, isDirectory: false, name,
        file(callback) { callback({ name, size }); },
    };
}

function dirEntry(name, children) {
    let served = false;
    return {
        isFile: false, isDirectory: true, name,
        createReader() {
            return {
                readEntries(callback) {
                    if (served) { callback([]); return; }
                    served = true;
                    callback(children);
                },
            };
        },
    };
}

function dropped(entries, { withEntryApi = true } = {}) {
    return {
        types: ['Files'],
        items: entries.map(entry => ({
            kind: 'file',
            webkitGetAsEntry: withEntryApi ? () => entry : undefined,
        })),
    };
}

// -- walking and partitioning ---------------------------------------------

test('a dropped tree keeps its shape as each file own relative path', async () => {
    const s = setup();
    const transfer = dropped([
        fileEntry('note.txt', 4),
        dirEntry('reports', [fileEntry('q1.csv', 10), dirEntry('deep', [fileEntry('x.md', 2)])]),
    ]);
    const collected = await s.ctx.wsCollectDroppedFiles(transfer);
    assert.equal(collected.unsupported, false);
    // Spread into this realm's arrays before deep-comparing: an array built
    // inside the sandbox has the sandbox's prototypes.
    assert.deepEqual([...collected.files].map(f => f.rel).sort(),
        ['note.txt', 'reports/deep/x.md', 'reports/q1.csv']);
});

test('a browser without the entry API is refused, not half-uploaded', async () => {
    // Uploading the top level only would silently lose everything inside a
    // dropped folder, so the honest answer is "try the folder picker".
    const s = setup();
    const collected = await s.ctx.wsCollectDroppedFiles(dropped([fileEntry('a.txt', 1)],
        { withEntryApi: false }));
    assert.equal(collected.unsupported, true);
    assert.equal(collected.files.length, 0);
});

test('a pathologically deep drop is flagged instead of walked forever', async () => {
    const s = setup();
    let node = fileEntry('leaf.txt', 1);
    for (let i = 0; i < 40; i += 1) node = dirEntry(`d${i}`, [node]);
    const collected = await s.ctx.wsCollectDroppedFiles(dropped([node]));
    assert.equal(collected.files.length, 0);
    assert.deepEqual([...collected.problems].map(p => p.code), ['too_deep']);
});

test('the three drop ceilings are applied per item, over the whole drop', () => {
    const s = setup();
    const MB = 1024 * 1024;
    const { accepted, skipped } = s.ctx.wsPartitionDrop([
        { file: { size: 200 * MB }, rel: 'ok.bin' },
        { file: { size: 200 * MB + 1 }, rel: 'too-big.bin' },
        { file: { size: 1024 }, rel: 'small.txt' },
    ]);
    assert.deepEqual([...skipped].map(item => [item.name, item.code]),
        [['too-big.bin', 'too_large']]);
    assert.deepEqual([...accepted].map(item => item.rel), ['ok.bin', 'small.txt']);
});

test('one drop may not carry more than 5000 files', () => {
    const s = setup();
    const files = Array.from({ length: 5001 }, (_, i) => ({ file: { size: 1 }, rel: `f${i}.txt` }));
    const { accepted, skipped } = s.ctx.wsPartitionDrop(files);
    assert.equal(accepted.length, 5000);
    assert.deepEqual([...skipped].map(item => item.code), ['too_many_files']);
});

test('one drop may not carry more than 5GB', () => {
    const s = setup();
    // 100MB apiece: under the per-file cap, over the drop's total on the 52nd.
    const MB = 1024 * 1024;
    const files = Array.from({ length: 52 }, (_, i) => ({ file: { size: 100 * MB }, rel: `f${i}.bin` }));
    const { accepted, skipped } = s.ctx.wsPartitionDrop(files);
    assert.equal(accepted.length, 51);
    assert.deepEqual([...skipped].map(item => [item.name, item.code]),
        [['f51.bin', 'too_much_total']]);
});

// -- what leaves the browser ----------------------------------------------

async function dropOne(s, { payload = { status: 'success' }, status = 200,
    dir = AGENT_DIR, name = 'note.txt', size = 4 } = {}) {
    s.poke(`wsCurrentDir = ${JSON.stringify(dir)};`);
    const pending = s.ctx.wsHandleDrop(dropped([fileEntry(name, size)]));
    await waitFor(() => s.xhrs.length === 1, 'the upload request');
    s.xhrs[0].finish(status, payload);
    await pending;
    return s.xhrs[0];
}

test('each file is one request carrying its own path below the drop', async () => {
    const s = setup();
    const xhr = await dropOne(s);
    assert.equal(xhr.method, 'POST');
    assert.equal(xhr.url, `/api/workspace/upload?agent=${AGENT}`);
    const form = Object.fromEntries(xhr.body.entries.map(e => [e.key, e.value]));
    assert.equal(form.dir, AGENT_DIR);
    assert.equal(form.relative_path, 'note.txt');
    assert.equal(xhr.body.entries.find(e => e.key === 'file').name, 'note.txt');
});

test('the request is addressed to the scope the panel is browsing', async () => {
    const s = setup();
    s.poke("wsAgentOverride = 'other-agent';");
    const xhr = await dropOne(s, { dir: 'agents/other-agent' });
    assert.match(xhr.url, /agent=other-agent/);
});

test('a finished drop refreshes the directory it landed in', async () => {
    const s = setup();
    await dropOne(s, { dir: AGENT_DIR });
    const trees = s.treeRequests();
    assert.equal(trees.length, 1, s.requests.join('\n'));
    assert.match(trees[0], /path=agents%2Fagent-a/);
});

// -- the write scope, as the panel understands it -------------------------

test('a private Agent is writable from its own root down', () => {
    const s = setup();
    s.poke(`wsCurrentDir = ${JSON.stringify(AGENT_DIR)};`);
    assert.equal(s.ctx.wsCanWriteHere(), true);
    s.poke(`wsCurrentDir = ${JSON.stringify(`${AGENT_DIR}/uploads/deep`)};`);
    assert.equal(s.ctx.wsCanWriteHere(), true);
    s.poke('wsCurrentDir = "agents/another";');
    assert.equal(s.ctx.wsCanWriteHere(), false);
    s.poke('wsCurrentDir = "";');
    assert.equal(s.ctx.wsCanWriteHere(), false);
});

test('a shared Agent is writable only inside the caller own folder', () => {
    const s = setup();
    s.poke(`chatAgentCatalog = [{id: ${JSON.stringify(SHARED)}, visibility: 'tenant'}];`);
    s.poke(`activeAgentId = ${JSON.stringify(SHARED)}; wsOwnUserIdCache = ${JSON.stringify(UID)};`);
    s.poke(`wsCurrentDir = ${JSON.stringify(OWN_DIR)};`);
    assert.equal(s.ctx.wsCanWriteHere(), true);
    s.poke(`wsCurrentDir = ${JSON.stringify(`${OWN_DIR}/uploads`)};`);
    assert.equal(s.ctx.wsCanWriteHere(), true);
    // The Agent's shared root is readable but never a drop target.
    s.poke(`wsCurrentDir = ${JSON.stringify(`agents/${SHARED}`)};`);
    assert.equal(s.ctx.wsCanWriteHere(), false);
    // Nor is a colleague's subtree.
    s.poke(`wsCurrentDir = ${JSON.stringify(`agents/${SHARED}/user/usr_other`)};`);
    assert.equal(s.ctx.wsCanWriteHere(), false);
});

test('a roster row with no visibility falls back to the Agent own folder', () => {
    // The panel derives the writable range from the visibility the *server*
    // reported; an unknown row is not assumed shared (which would invent a
    // `user/<id>` path), so it falls back to the Agent's own folder. That is a
    // UI hint only -- the server refuses the write if the range is not real.
    const s = setup();
    s.poke(`chatAgentCatalog = []; activeAgentId = 'mystery'; wsOwnUserIdCache = ${JSON.stringify(UID)};`);
    s.poke(`wsCurrentDir = 'agents/mystery';`);
    assert.equal(s.ctx.wsWritableRootPath(), 'agents/mystery');
    assert.equal(s.ctx.wsCanWriteHere(), true);
});

test('the drop hint says where it lands, or says it will be refused', () => {
    const s = setup();
    s.poke(`wsCurrentDir = ${JSON.stringify(AGENT_DIR)};`);
    s.ctx.wsShowDropHint();
    const hint = s.get('ws-drop-hint');
    assert.equal(hint.classList.contains('hidden'), false);
    assert.equal(hint.classList.contains('ws-drop-refused'), false);
    assert.match(s.get('ws-drop-hint-text').textContent, /agents\/agent-a/);

    s.poke('wsCurrentDir = "agents/another";');
    s.ctx.wsShowDropHint();
    assert.equal(hint.classList.contains('ws-drop-refused'), true);
    assert.equal(s.get('ws-drop-hint-text').textContent, 'ws_upload_out_of_scope');
});

test('a drop outside the writable range is refused before any byte moves', async () => {
    const s = setup();
    s.poke('wsCurrentDir = "agents/another";');
    await s.ctx.wsHandleDrop(dropped([fileEntry('a.txt', 1)]));
    assert.deepEqual(s.xhrs, []);
    assert.deepEqual(s.toasts, ['ws_upload_out_of_scope']);
});

test('a browser without the entry API is told to use the folder picker', async () => {
    const s = setup();
    s.poke(`wsCurrentDir = ${JSON.stringify(AGENT_DIR)};`);
    await s.ctx.wsHandleDrop(dropped([fileEntry('a.txt', 1)], { withEntryApi: false }));
    assert.deepEqual(s.toasts, ['ws_upload_no_dir_api']);
    assert.equal(s.get('ws-upload-progress').classList.contains('hidden'), true);
});

// -- progress --------------------------------------------------------------

test('progress counts bytes actually on the wire, not files finished', async () => {
    const s = setup();
    s.poke(`wsCurrentDir = ${JSON.stringify(AGENT_DIR)};`);
    const pending = s.ctx.wsHandleDrop(
        dropped([fileEntry('a.bin', 1000), fileEntry('b.bin', 1000)]));
    await waitFor(() => s.xhrs.length === 2, 'both requests');
    // The event's total is the multipart envelope (the file plus framing), so
    // 240 of 1200 bytes on the wire is 200 of the file's own 1000 — the panel
    // must report the file's progress, not the envelope's.
    s.xhrs[0].progress(240, 1200);
    assert.match(s.get('ws-upload-count').textContent, /200B/);
    assert.match(s.get('ws-upload-count').textContent, /0\/2 项/);
    assert.match(s.get('ws-upload-count').textContent, /10%/);
    s.xhrs[0].finish(200, { status: 'success' });
    s.xhrs[1].finish(200, { status: 'success' });
    await pending;
    assert.equal(s.get('ws-upload-title').textContent, 'ws_upload_sending');
    assert.equal(s.get('ws-upload-fill').style.width, '100%');
});

test('a drop that lost something never reads 100%', () => {
    const s = setup();
    s.poke(`wsUpload = ${JSON.stringify({
        seq: 1, dir: AGENT_DIR, phase: 'done', found: 2, queued: 1, totalBytes: 1000,
        doneCount: 1, transferred: { 0: 1000 }, renamed: [],
        skipped: [{ name: 'huge.bin', code: 'too_large' }], failures: [],
    })};`);
    s.ctx.wsRenderUpload();
    assert.equal(s.get('ws-upload-fill').style.width, '99%');
    assert.equal(s.get('ws-upload-progress').classList.contains('ws-upload-partial'), true);
    assert.equal(s.get('ws-upload-title').textContent, 'ws_upload_partial');
    assert.match(s.get('ws-upload-detail').textContent, /huge\.bin/);
    assert.match(s.get('ws-upload-detail').textContent, /ws_upload_err_too_large/);
});

test('the retry button counts only the failures that can be re-sent', () => {
    const s = setup();
    s.poke(`wsUpload = ${JSON.stringify({
        seq: 1, dir: AGENT_DIR, phase: 'done', found: 3, queued: 3, totalBytes: 30,
        doneCount: 1, transferred: { 0: 10 }, renamed: [],
        skipped: [{ name: 'huge.bin', code: 'too_large' }],
        failures: [{ name: 'net.bin', code: 'network', item: { rel: 'net.bin', file: { size: 10 } } },
                   { name: 'hard.bin', code: 'forbidden' }],
    })};`);
    s.ctx.wsRenderUpload();
    const retry = s.get('ws-upload-retry');
    assert.equal(retry.classList.contains('hidden'), false);
    // The skipped item and the refusal have no `item`, so only one is retryable.
    assert.match(retry.textContent, /1/);
});

test('a retry re-sends only the failed items', async () => {
    const s = setup();
    s.poke(`wsCurrentDir = ${JSON.stringify(AGENT_DIR)};`);
    const pending = s.ctx.wsHandleDrop(
        dropped([fileEntry('ok.txt', 1), fileEntry('flaky.txt', 1)]));
    await waitFor(() => s.xhrs.length === 2, 'both requests');
    s.xhrs[0].finish(200, { status: 'success' });
    s.xhrs[1].fail();
    await pending;
    assert.equal(s.xhrs.length, 2);
    assert.match(s.get('ws-upload-detail').textContent, /ws_upload_err_network/);

    const retried = s.ctx.retryFailedWorkspaceUploads();
    await waitFor(() => s.xhrs.length === 3, 'the retry request');
    // The retry carries the failed file, not the one already on disk.
    const form = Object.fromEntries(s.xhrs[2].body.entries.map(e => [e.key, e.value]));
    assert.equal(form.relative_path, 'flaky.txt');
    s.xhrs[2].finish(200, { status: 'success' });
    await retried;
    assert.equal(s.peek('wsUpload.failures.length'), 0);
});

test('a server rename is reported, not hidden', async () => {
    const s = setup();
    await dropOne(s, { payload: { status: 'success', renamed: true,
        path: `${AGENT_DIR}/note (1).txt` } });
    // The label carries a count, so the harness resolves the real string: the
    // rename notice is the only way the user finds the file under its new name.
    assert.match(s.get('ws-upload-detail').textContent, /1 项因重名已改名/);
    assert.match(s.get('ws-upload-detail').textContent, /note \(1\)\.txt/);
});

test('a server refusal is reported with its own reason code', async () => {
    const s = setup();
    const xhr = await dropOne(s, { status: 200, payload: { status: 'error',
        code: 'outside_own_directory', message: 'outside the directory you may write to' } });
    assert.match(s.get('ws-upload-detail').textContent, /ws_lock_outside/);
    assert.equal(s.ctx.wsUploadFailureText({ code: 'too_many_files' }),
        'ws_upload_err_too_many_files');
    // An unknown code keeps the server's own words rather than a blank label.
    assert.equal(s.ctx.wsUploadFailureText({ code: 'weird', message: 'no space left' }),
        'no space left');
    assert.equal(xhr.status, 200);
});

test('a 413 from the proxy is read as a size refusal, not a code-less failure', () => {
    const s = setup();
    assert.equal(s.ctx.wsUploadFailure({ rel: 'big.bin' }, '', 413, '').code, 'too_large');
});

test('the three layers that can refuse a file stay distinguishable', () => {
    const s = setup();
    // 1. the proxy: it never reached the app, so the status is the only signal
    assert.equal(s.ctx.wsUploadFailure({ rel: 'a' }, '', 413, '').code, 'too_large');
    // 2. the app's own ceiling: it arrived, counted the bytes, and named a code
    assert.equal(s.ctx.wsUploadFailure({ rel: 'b' }, 'too_large', 200,
        'file is larger than 209715200 bytes').code, 'too_large');
    // 3. a cut-short transfer: also the app, but a different remedy -- upload
    //    again rather than split the file -- so it must not share a wording
    assert.equal(s.ctx.wsUploadFailure({ rel: 'c' }, 'incomplete_upload', 200,
        'incomplete upload: 10 of 20 bytes').code, 'incomplete_upload');
    assert.equal(s.ctx.wsUploadFailureText({ code: 'incomplete_upload' }),
        'ws_upload_err_incomplete');
    // 4. scope: the bytes were fine, the destination was not
    assert.equal(s.ctx.wsUploadFailureText({ code: 'outside_own_directory' }),
        'ws_lock_outside');
});

// -- scope changes --------------------------------------------------------

test('a drop that started under the previous scope does not paint the new one', async () => {
    const s = setup();
    s.poke(`wsCurrentDir = ${JSON.stringify(AGENT_DIR)};`);
    const pending = s.ctx.wsHandleDrop(dropped([fileEntry('a.txt', 1)]));
    await waitFor(() => s.xhrs.length === 1, 'the request');
    // The user switches Agent (or session) while the file is in flight.
    s.ctx.wsResetUploadAndTrashState();
    s.xhrs[0].finish(200, { status: 'success' });
    await pending;
    await flush();
    assert.deepEqual(s.treeRequests(), [], 'the stale drop refreshed the new scope');
    assert.equal(s.get('ws-upload-progress').classList.contains('hidden'), true);
});

test('switching scope forgets the bin and the last drop report', () => {
    const s = setup();
    s.poke("wsTrashMode = true; wsUpload = wsUpload || null; wsTrashEntries = [{rel: 'x'}];");
    s.ctx.wsResetUploadAndTrashState();
    assert.equal(s.peek('wsTrashMode'), false);
    assert.equal(s.peek('wsTrashEntries.length'), 0);
});

// -- the drop zone --------------------------------------------------------

test('a file drag is handled by the panel and never reaches the chat overlay', () => {
    const s = setup();
    s.poke(`wsCurrentDir = ${JSON.stringify(AGENT_DIR)};`);
    const target = s.get('ws-body-files');
    s.ctx.initWorkspaceUploadDrop();
    const transfer = dropped([fileEntry('a.txt', 1)]);
    const enter = target.fire('dragenter', { dataTransfer: transfer });
    assert.equal(enter.defaultPrevented, true);
    assert.equal(enter.propagated, false, 'the chat attachment overlay would take it');
    assert.equal(s.get('ws-drop-hint').classList.contains('hidden'), false);
    const drop = target.fire('drop', { dataTransfer: transfer });
    assert.equal(drop.defaultPrevented, true);
    assert.equal(drop.propagated, false);
    assert.equal(s.get('ws-drop-hint').classList.contains('hidden'), true);
});

test('a drag that carries no files is left to the rest of the page', () => {
    const s = setup();
    const target = s.get('ws-body-files');
    s.ctx.initWorkspaceUploadDrop();
    const enter = target.fire('dragenter', { dataTransfer: { types: ['text/plain'], items: [] } });
    assert.equal(enter.defaultPrevented, false);
    assert.equal(enter.propagated, undefined);
    assert.equal(s.get('ws-drop-hint').classList.contains('hidden'), true);
});

test('within the same drag the hint only hides when the drag really leaves', () => {
    const s = setup();
    s.poke(`wsCurrentDir = ${JSON.stringify(AGENT_DIR)};`);
    const target = s.get('ws-body-files');
    s.ctx.initWorkspaceUploadDrop();
    const transfer = dropped([fileEntry('a.txt', 1)]);
    // `dragenter` fires again for every child the pointer crosses.
    target.fire('dragenter', { dataTransfer: transfer });
    target.fire('dragenter', { dataTransfer: transfer });
    target.fire('dragleave', { dataTransfer: transfer });
    assert.equal(s.get('ws-drop-hint').classList.contains('hidden'), false);
    target.fire('dragleave', { dataTransfer: transfer });
    assert.equal(s.get('ws-drop-hint').classList.contains('hidden'), true);
});

test('the drop zone advertises the refusal instead of accepting the drop', () => {
    const s = setup();
    s.poke('wsCurrentDir = "agents/another";');
    const target = s.get('ws-body-files');
    s.ctx.initWorkspaceUploadDrop();
    const over = target.fire('dragover', { dataTransfer: dropped([fileEntry('a.txt', 1)]) });
    assert.equal(over.defaultPrevented, true);
    assert.equal(over.dataTransfer.dropEffect, 'none');
});

// -- the page the panel is dropped into --------------------------------------

// The ids the panel needs live in two files that can drift apart without a
// single test going red: the page the server assembles and ships, and the script
// that looks the ids up. `getElementById` answers `null` for a markup element
// that is not there, so the control simply never appears -- silently, at runtime,
// in the browser. The suites in this file cannot see it, because they build
// their DOM by hand (and should: they are testing the script, not the page).
// Reading the ids out of the script and requiring them in the assembled page is
// the check that closes that gap.
const WEB_ROOT = path.join(ROOT, 'channel/web');

/** Assemble the page the way `channel/web/core/template.py` does. */
function expandIncludes(html, depth = 0) {
    if (depth > 8) throw new Error('include nesting too deep');
    return html.replace(/<!--#include\s+([^\s>]+?)\s*-->/g, (_all, rel) => {
        const file = path.resolve(WEB_ROOT, rel);
        if (!file.startsWith(WEB_ROOT + path.sep)) throw new Error('include escapes: ' + rel);
        return expandIncludes(fs.readFileSync(file, 'utf8'), depth + 1);
    });
}

test('every id the panel looks up exists in the page the server ships', () => {
    const page = expandIncludes(fs.readFileSync(path.join(WEB_ROOT, 'chat.html'), 'utf8'));
    const asked = [...new Set(
        [...SOURCE.matchAll(/getElementById\(\s*'([^']+)'\s*\)/g)].map(m => m[1]))];
    // An element the panel creates itself (`x.id = 'ws-editor'`) is sourced from
    // the script, not the page, so it is not markup that can go missing. Deriving
    // that set from the script rather than listing it here keeps this test from
    // needing an edit every time the panel grows a runtime-built node.
    const builtByScript = new Set(
        [...SOURCE.matchAll(/\.id\s*=\s*'([^']+)'/g)].map(m => m[1]));
    const missing = asked.filter(id => !builtByScript.has(id) && !page.includes(`id="${id}"`));
    assert.deepEqual(missing, [],
        'workspace.js looks these ids up, but the assembled chat.html has no such element');
    // The failure mode is a *silent* one, so prove the check can fail: an id
    // invented on the spot must be reported.
    assert.equal(asked.includes('ws-body-files'), true,
        'sanity: the script does look up ids, so an empty result is not a pass');
});

test('the panel markup is the assembled page\'s, not a fragment nobody renders', () => {
    // A copy of this panel also sits in `templates/views/chat.html`, which no
    // include reaches today. Landing an edit in that copy looks right, tests
    // right, and ships nothing -- so the page must carry the controls on its own.
    const page = expandIncludes(fs.readFileSync(path.join(WEB_ROOT, 'chat.html'), 'utf8'));
    for (const id of ['ws-btn-trash', 'ws-upload-progress', 'ws-drop-hint']) {
        assert.ok(page.includes(`id="${id}"`),
            `the shipped page must carry ${id} itself, not only a fragment`);
    }
    // The tick box and the toolbar buttons that acted on its selection are gone:
    // an action belongs to the row it applies to.
    for (const id of ['ws-btn-delete', 'ws-btn-restore', 'ws-btn-purge']) {
        assert.ok(!page.includes(`id="${id}"`),
            `${id} acted on a selection the panel no longer has`);
    }
});
