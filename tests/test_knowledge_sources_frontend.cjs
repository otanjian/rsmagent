// The knowledge page's "原始资料" tab: list, detail, upload and lifecycle
// (change add-traceable-knowledge-ingestion, tasks 3.2 and 3.3).
//
// Two things are easy to get wrong here and are what most of this file pins:
//
//   * "saved" is not "searchable". An original that has no converted body is
//     stored for download and audit, and is deliberately absent from every
//     search and document surface. The list and the detail must say so, and
//     must not let a second Agent's cache state be reported as this Agent's.
//   * a late response is another library's answer. Switching the viewed Agent
//     or navigating away mid-request must not paint the sources of the base the
//     user just left, and polling must only run while the tab is on screen.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { createDocument } = require('./_console_dom.cjs');

const ROOT = path.join(__dirname, '..');
const CONSOLE = path.join(ROOT, 'channel/web/static/js/console.js');
const CHAT_HTML = path.join(ROOT, 'channel/web/chat.html');
const source = fs.readFileSync(CONSOLE, 'utf8');

//: The dictionary the page ships. The harness's `t` is the identity so an
//: assertion can name a key, but a key that carries `{placeholders}` cannot
//: survive that: the value substituted into it is exactly what the user is
//: agreeing to (how many versions a delete removes), so those keys resolve for
//: real. Anything else would let a missing substitution pass unnoticed.
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

const FROM = '// --- Knowledge sources';
// The slice runs to the end of the knowledge view's own code (the d3 loader
// starts the graph module). It deliberately includes the tab switcher: which
// panel is visible and whether polling may run are the same question.
const TO = 'function ensureD3Loaded';

function sliceSources() {
    const start = source.indexOf(FROM);
    const end = source.indexOf(TO, start);
    assert.ok(start >= 0 && end > start, 'the knowledge-sources section exists');
    return source.slice(start, end);
}

/** Every `knowledge-*` element id the sources code reaches for by name. */
function addressedIds(code) {
    return [...new Set([...code.matchAll(/getElementById\(\s*'([^']+)'/g)].map(m => m[1]))]
        // `getElementById('knowledge-tab-' + tab)` builds its id at runtime, so
        // the literal prefix is not an id the markup can carry.
        .filter(id => !id.endsWith('-'));
}

/** The panel ids the markup must carry, whatever the implementation does. */
const REQUIRED_IDS = [
    'knowledge-tab-sources', 'knowledge-panel-docs', 'knowledge-panel-sources',
    'knowledge-sources-list',
    'knowledge-sources-empty', 'knowledge-sources-loading', 'knowledge-sources-error',
    'knowledge-sources-usage', 'knowledge-sources-note',
    'knowledge-sources-filters', 'knowledge-sources-filter',
    'knowledge-sources-category', 'knowledge-sources-ext', 'knowledge-sources-status',
    'knowledge-sources-no-match',
    'knowledge-upload-btn', 'knowledge-upload-panel', 'knowledge-upload-queue',
    'knowledge-upload-category', 'knowledge-upload-conflict', 'knowledge-upload-convert',
    'knowledge-upload-input', 'knowledge-upload-submit', 'knowledge-upload-summary',
    'knowledge-upload-limits', 'knowledge-upload-target',
    'knowledge-source-detail', 'knowledge-source-detail-title',
    'knowledge-source-detail-state', 'knowledge-source-detail-body',
    'knowledge-source-detail-placeholder',
];

class FakeXHR {
    constructor(registry) {
        this.registry = registry;
        this.upload = {};
        this.headers = {};
    }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(key, value) { this.headers[key] = value; }
    send(body) { this.registry.push(this); this.body = body; }
    /** Answer the request as the server would. */
    finish(status, payload) {
        this.status = status;
        this.responseText = JSON.stringify(payload);
        if (this.onload) this.onload();
    }
    progress(loaded, total) {
        if (this.upload.onprogress) {
            this.upload.onprogress({ lengthComputable: true, loaded, total });
        }
    }
}

function setup(payloads = {}, authCtx = { status: 'success', effective_permissions: ['knowledge.read'] }) {
    const document = createDocument();
    const requests = [];
    const hash = {};

    for (const id of REQUIRED_IDS) document.register(id, document.createElement('div'));
    document.getElementById('knowledge-sources-list').classList.add('hidden');
    document.getElementById('knowledge-sources-empty').classList.add('hidden');
    document.getElementById('knowledge-sources-error').classList.add('hidden');

    const location = { href: '', hash: '' };
    // The delete confirmation's own text is part of what the user agrees to, so
    // the harness records it instead of only answering yes/no.
    const confirmMessages = [];
    const confirmState = { value: true };
    const confirmFn = message => { confirmMessages.push(message); return confirmState.value; };
    // Mirrors the shell's `_kbUrl`: append the viewed Agent, never a duplicate.
    const kbUrl = path => `${path}${path.includes('?') ? '&' : '?'}agent_id=agent-a`;
    const ctx = {
        currentLang: 'zh',
        currentView: 'knowledge',
        t: labelFor,
        escapeHtml: x => String(x == null ? '' : x),
        activeAgentId: 'agent-a',
        defaultAgentId: 'agent-a',
        agentCatalog: [{ id: 'agent-a', name: 'A' }, { id: 'agent-b', name: 'B' }],
        findAgent: id => (id === 'agent-b' ? { id: 'agent-b', name: 'B' } : { id: 'agent-a', name: 'A' }),
        _setKnowledgeStatus: (message, isError) => { hash.status = { message, isError }; },
        // The viewer's Agent and the knowledge URL builder live outside the
        // section under test (they belong to the page shell), so they are
        // stubbed to the same contract the shell honours: every knowledge call
        // is addressed to the Agent the page is showing.
        viewingKnowledgeAgentId: () => 'agent-a',
        _kbUrl: kbUrl,
        URLSearchParams,
        FormData: class { constructor() { this.entries = []; } append(k, v) { this.entries.push([k, v]); } },
        XMLHttpRequest: function () { return new FakeXHR(requests); },
        location,
        confirm: confirmFn,
        window: { confirm: confirmFn, innerWidth: 1280 },
        document,
        _baseAuthContext: () => authCtx,
    };
    ctx.window.location = location;
    vm.createContext(ctx);
    const code = sliceSources();
    vm.runInContext(code, ctx);
    return {
        ctx, document, requests, location,
        hash, confirmMessages,
        get: id => ctx.document.getElementById(id),
        code,
        setConfirm: answer => { confirmState.value = answer; },
    };
}

const LIST = {
    status: 'success',
    sources: [
        {
            source_id: 'src-1', name: '合同.pdf', category: 'legal', lifecycle: 'active',
            latest_version: 2, active_version: 1, active_task_id: 'task-9',
            target_task_id: 'task-9', converted: true, searchable: true, size: 2048,
            created_at: '2026-09-24T03:00:00Z', updated_at: '2026-09-26T02:00:00Z',
            latest_task: { task_id: 'task-9', task_type: 'convert', status: 'complete', stage: 'indexed', error: null },
        },
        {
            source_id: 'src-2', name: '报价.xlsx', category: '', lifecycle: 'active',
            latest_version: 1, active_version: 0, active_task_id: null,
            target_task_id: null, converted: false, searchable: false, size: 512,
            latest_task: null,
        },
    ],
    registered: true,
    capabilities: {
        knowledge_enabled: true, can_write: true,
        source_upload: { configured: true, dependency_ready: true, available: true, reason: '' },
        conversion: { configured: false, dependency_ready: false, available: false, reason: 'conversion is off' },
    },
    limits: { max_files: 100, max_file_size: 10485760, max_batch_size: 209715200, storage_quota: 0 },
};

const DETAIL = {
    status: 'success',
    source: LIST.sources[1],
    versions: [
        { version: 1, original_name: '报价.xlsx', size: 512, content_hash: 'a'.repeat(64), commit_state: 'committed', created_at: '2026-09-26T01:00:00Z', ext: '.xlsx' },
    ],
    tasks: [
        { task_id: 'task-11', task_type: 'convert', target_version: 1, status: 'failed', stage: 'extract', error: 'encrypted', manifest: null, created_at: '2026-09-26T01:01:00Z', updated_at: '2026-09-26T01:02:00Z' },
    ],
    limits: LIST.limits,
};

const flush = () => new Promise(resolve => setImmediate(resolve));

/** A harness whose `fetch` answers from `payload(url)`. */
function jsonResponse(payload) {
    const s = setup();
    s.ctx.fetch = async url => ({ json: async () => payload(url) });
    return s;
}

test('the sources tab shows its own panel and loads the list', async () => {
    const s = jsonResponse(url => (url.includes('/detail') ? DETAIL : LIST));
    s.ctx.switchKnowledgeTab('sources');
    assert.equal(s.get('knowledge-panel-sources').classList.contains('hidden'), false,
        'the sources panel is shown');
    assert.equal(s.get('knowledge-panel-docs').classList.contains('hidden'), true);
    await flush();
    const html = s.get('knowledge-sources-list').innerHTML;
    assert.match(html, /合同\.pdf/);
    assert.match(html, /报价\.xlsx/);
    assert.match(html, /knowledge_sources_state_searchable/, 'a converted source says it is searchable');
    assert.match(html, /knowledge_sources_state_saved/, 'a stored-only source says saved, not searchable');
});

test('the list addresses the current Agent, the same library the documents tab shows', async () => {
    const s = jsonResponse(() => LIST);
    const urls = [];
    s.ctx.fetch = async url => { urls.push(url); return { json: async () => LIST }; };
    s.ctx.loadKnowledgeSources();
    await flush();
    assert.match(urls[0], /\/api\/knowledge\/sources(\?|$)/);
    assert.match(urls[0], /agent_id=agent-a/);
});

test('an empty library shows the empty state instead of a blank panel', async () => {
    const s = jsonResponse(() => ({ ...LIST, sources: [] }));
    s.ctx.loadKnowledgeSources();
    await flush();
    assert.equal(s.get('knowledge-sources-empty').classList.contains('hidden'), false);
    assert.equal(s.get('knowledge-sources-list').classList.contains('hidden'), true);
    assert.equal(s.get('knowledge-sources-loading').classList.contains('hidden'), true);
});

test('a refused read shows the reason in the panel, not a spinner', async () => {
    const s = jsonResponse(() => ({ status: 'error', code: 'forbidden', message: 'no access' }));
    s.ctx.loadKnowledgeSources();
    await flush();
    assert.equal(s.get('knowledge-sources-error').classList.contains('hidden'), false);
    assert.match(s.get('knowledge-sources-error').textContent, /no access/);
    assert.equal(s.get('knowledge-sources-loading').classList.contains('hidden'), true);
});

test('the upload button appears only when the deployment can actually store originals', async () => {
    const on = jsonResponse(() => LIST);
    on.ctx.loadKnowledgeSources();
    await flush();
    assert.equal(on.get('knowledge-upload-btn').classList.contains('hidden'), false);

    const off = jsonResponse(() => ({
        ...LIST,
        capabilities: {
            ...LIST.capabilities,
            source_upload: { configured: false, dependency_ready: true, available: false, reason: 'not enabled' },
        },
    }));
    off.ctx.loadKnowledgeSources();
    await flush();
    assert.equal(off.get('knowledge-upload-btn').classList.contains('hidden'), true,
        'a deployment with the switch off does not offer a dead button');
    assert.equal(off.get('knowledge-upload-btn').getAttribute('title'), 'not enabled',
        'and the server reason is on the hidden control, for the tooltip that explains it');
});

test('a late list response never repaints the panel after a newer load', async () => {
    let releaseFirst;
    const first = new Promise(resolve => { releaseFirst = () => resolve({ json: async () => LIST }); });
    const s = setup();
    let call = 0;
    s.ctx.fetch = async () => (++call === 1 ? first : { json: async () => ({ ...LIST, sources: [] }) });

    s.ctx.loadKnowledgeSources();
    s.ctx.loadKnowledgeSources();
    await flush();
    releaseFirst();
    await flush();

    assert.equal(s.get('knowledge-sources-empty').classList.contains('hidden'), false,
        'the second answer owns the panel');
    assert.equal(s.get('knowledge-sources-list').innerHTML, '',
        'the stale first answer did not paint its older rows');
});

test('opening a source renders versions, tasks and its own lifecycle actions', async () => {
    const s = jsonResponse(url => (url.includes('/detail') ? DETAIL : LIST));
    s.ctx.loadKnowledgeSources();
    await flush();
    await s.ctx.openKnowledgeSourceDetail('src-2');
    await flush();

    const body = s.get('knowledge-source-detail-body').innerHTML;
    assert.match(body, /报价\.xlsx/, 'the original file name is shown');
    assert.match(body, /downloadKnowledgeSource\(&#39;src-2&#39;|downloadKnowledgeSource\('src-2'/,
        'the original is downloadable');
    assert.match(body, /knowledge_sources_retry/, 'a failed task offers a retry');
    assert.match(body, /knowledge_sources_disable/, 'an active source offers disable');
    assert.match(body, /encrypted/, 'the task error is shown instead of a bare "failed"');
    assert.equal(s.get('knowledge-source-detail').classList.contains('hidden'), false);
    assert.equal(s.get('knowledge-source-detail-placeholder').classList.contains('hidden'), true);
});

test('the detail refuses to offer conversion when the deployment cannot convert', async () => {
    const s = jsonResponse(url => (url.includes('/detail') ? DETAIL : LIST));
    s.ctx.loadKnowledgeSources();
    await flush();
    await s.ctx.openKnowledgeSourceDetail('src-2');
    await flush();
    const body = s.get('knowledge-source-detail-body').innerHTML;
    assert.doesNotMatch(body, /requestKnowledgeSourceConversion\(\s*['"]src-2/,
        'no convert action is wired while conversion is unavailable');
    assert.match(body, /conversion is off/, 'the reason is stated instead');
});

test('a download is a browser navigation to the tenant-addressed route', () => {
    const s = setup();
    s.ctx.downloadKnowledgeSource('src-1', 2);
    assert.match(s.location.href, /^\/api\/knowledge\/sources\/download\?/);
    assert.match(s.location.href, /source_id=src-1/);
    assert.match(s.location.href, /version=2/);
    assert.match(s.location.href, /agent_id=agent-a/);
});

test('delete asks first, then posts the lifecycle action and refreshes', async () => {
    const s = jsonResponse(url => (url.includes('/detail') ? DETAIL : LIST));
    s.ctx.loadKnowledgeSources();
    await flush();
    s.setConfirm(false);
    assert.equal(await s.ctx.setKnowledgeSourceLifecycle('src-1', 'delete'), null);
    assert.equal(s.requests.length, 0, 'a declined delete sends nothing');

    s.setConfirm(true);
    await s.ctx.setKnowledgeSourceLifecycle('src-1', 'delete');
    const posted = s.requests[s.requests.length - 1];
    assert.match(posted.url, /\/api\/knowledge\/sources\/lifecycle$/);
    assert.deepEqual(JSON.parse(posted.body),
        { source_id: 'src-1', action: 'delete', agent_id: 'agent-a' });
});

test('retry and cancel post the task action', async () => {
    const s = jsonResponse(() => LIST);
    await s.ctx.retryKnowledgeSourceTask('task-11');
    assert.deepEqual(JSON.parse(s.requests[0].body),
        { task_id: 'task-11', action: 'retry', agent_id: 'agent-a' });
    await s.ctx.cancelKnowledgeSourceTask('task-11');
    assert.deepEqual(JSON.parse(s.requests[1].body),
        { task_id: 'task-11', action: 'cancel', agent_id: 'agent-a' });
});

test('the upload panel queues files and reports per-item progress', async () => {
    const s = jsonResponse(() => LIST);
    await s.ctx.loadKnowledgeSources();
    await flush();
    s.ctx.openKnowledgeUploadPanel();
    assert.equal(s.get('knowledge-upload-panel').classList.contains('hidden'), false);
    assert.match(s.get('knowledge-upload-limits').textContent, /100/,
        'the server-projected limits are shown');

    s.ctx.addKnowledgeUploadFiles([
        { name: 'a.pdf', size: 10 },
        { name: 'b.xlsx', size: 20 },
    ]);
    assert.match(s.get('knowledge-upload-queue').innerHTML, /a\.pdf/);
    assert.match(s.get('knowledge-upload-queue').innerHTML, /b\.xlsx/);

    s.ctx.submitKnowledgeUpload();
    const xhr = s.requests[0];
    assert.match(xhr.url, /\/api\/knowledge\/sources\/upload$/);
    xhr.progress(15, 30);
    assert.match(s.get('knowledge-upload-queue').innerHTML, /50%/, 'progress is per batch item');
    xhr.finish(200, {
        status: 'success', saved: 1, reused: 1, conflict: 0, failed: 0,
        results: [
            { filename: 'a.pdf', status: 'saved', source_id: 'src-a', version: 1 },
            { filename: 'b.xlsx', status: 'reused', reason: 'duplicate_content', source_id: 'src-b', version: 1 },
        ],
    });
    await flush();
    const queue = s.get('knowledge-upload-queue').innerHTML;
    assert.match(queue, /knowledge_upload_item_saved/);
    assert.match(queue, /knowledge_upload_item_reused/);
});

test('a same-name conflict becomes a per-item choice instead of an overwrite', async () => {
    const s = jsonResponse(() => LIST);
    await s.ctx.loadKnowledgeSources();
    await flush();
    s.ctx.openKnowledgeUploadPanel();
    s.ctx.addKnowledgeUploadFiles([{ name: '合同.pdf', size: 10 }]);
    s.ctx.submitKnowledgeUpload();
    s.requests[0].finish(200, {
        status: 'success', saved: 0, reused: 0, conflict: 1, failed: 0,
        results: [{
            filename: '合同.pdf', status: 'conflict', reason: 'name_conflict',
            existing: { source_id: 'src-1', name: '合同.pdf', category: 'legal', latest_version: 2 },
        }],
    });
    await flush();
    const queue = s.get('knowledge-upload-queue').innerHTML;
    assert.match(queue, /resolveKnowledgeUploadConflict\(0, &#39;new&#39;|resolveKnowledgeUploadConflict\(0, 'new'/);
    assert.match(queue, /resolveKnowledgeUploadConflict\(0, &#39;update&#39;|resolveKnowledgeUploadConflict\(0, 'update'/);

    s.ctx.resolveKnowledgeUploadConflict(0, 'update');
    const xhr = s.requests[s.requests.length - 1];
    const parts = xhr.body.entries.filter(([key]) => key !== 'files');
    const sent = Object.fromEntries(parts);
    assert.equal(sent.conflict, 'update');
    assert.equal(sent.target_source_id, 'src-1');
    assert.equal(sent.expected_version, '2', 'the version the choice was made against');
    assert.ok(sent.request_id, 'a stable request id travels with the retry');
});

test('an oversized file fails locally and never reaches the server', async () => {
    const s = jsonResponse(() => LIST);
    await s.ctx.loadKnowledgeSources();
    await flush();
    s.ctx.openKnowledgeUploadPanel();
    s.ctx.addKnowledgeUploadFiles([{ name: 'huge.bin', size: 99 * 1024 * 1024 }]);
    const queue = s.get('knowledge-upload-queue').innerHTML;
    assert.match(queue, /knowledge_upload_item_failed/);
    await s.ctx.submitKnowledgeUpload();
    assert.equal(s.requests.length, 0, 'nothing is submitted for a file the server would refuse');
});

test('polling runs only while the sources tab is on screen', async () => {
    const timers = [];
    const s = jsonResponse(() => LIST);
    s.ctx.setInterval = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
    s.ctx.clearInterval = id => { if (timers[id - 1]) timers[id - 1].cleared = true; };

    s.ctx.loadKnowledgeSources();
    await flush();
    // src-1 is fully converted and src-2 has no task: nothing is in flight, so
    // there is no timer to run at all.
    assert.equal(timers.filter(timer => !timer.cleared).length, 0,
        'a settled list does not poll');

    s.ctx.stopKnowledgeSourcePolling();
    s.ctx.switchKnowledgeTab('docs');
    const calls = s.requests.length;
    timers.forEach(timer => timer.fn());
    assert.equal(s.requests.length, calls, 'a timer never fires into a hidden tab');
});

test('switching the viewed Agent drops the old selection and stops polling', async () => {
    const s = jsonResponse(url => (url.includes('/detail') ? DETAIL : LIST));
    s.ctx.loadKnowledgeSources();
    await flush();
    await s.ctx.openKnowledgeSourceDetail('src-2');
    await flush();
    assert.equal(s.ctx._knowledgeSourceSelectedId, 'src-2');
    // What the page's Agent selector runs before it reloads the view.
    s.ctx.knowledgeSourcesOnLibraryChange();
    assert.equal(s.ctx._knowledgeSourceSelectedId, '', 'no row stays selected across libraries');
    assert.equal(s.get('knowledge-source-detail').classList.contains('hidden'), true,
        'and the detail of the library just left is not left on screen');
    assert.equal(s.get('knowledge-sources-list').innerHTML, '',
        'nor are its rows: the panel goes back to "loading" for the new library');
});

test('the page Agent selector resets the sources tab', () => {    // The reset itself is covered above; this pins the wiring, because a
    // selector that reloads the view but keeps the sources state would show one
    // library's rows under another library's name.
    const start = source.indexOf('function selectKnowledgeAgent');
    const end = source.indexOf('function canWriteKnowledge', start);
    assert.ok(start >= 0 && end > start, 'selectKnowledgeAgent is where we think it is');
    assert.match(source.slice(start, end), /knowledgeSourcesOnLibraryChange\(\)/,
        'selecting another Agent drops the previous library\'s sources state');
});

test('the list filters by name, category, type and status locally', async () => {
    const s = jsonResponse(() => LIST);
    s.ctx.loadKnowledgeSources();
    await flush();
    const rows = () => s.get('knowledge-sources-list').innerHTML;

    assert.match(rows(), /合同\.pdf/);
    assert.match(rows(), /报价\.xlsx/);

    s.get('knowledge-sources-filter').value = '报价';
    s.ctx.renderKnowledgeSources();
    assert.match(rows(), /报价\.xlsx/);
    assert.doesNotMatch(rows(), /合同\.pdf/, 'a name search hides the non-matching row');

    s.get('knowledge-sources-filter').value = '';
    s.get('knowledge-sources-status').value = 'searchable';
    s.ctx.renderKnowledgeSources();
    assert.match(rows(), /合同\.pdf/);
    assert.doesNotMatch(rows(), /报价\.xlsx/, 'status filters on whether a body is published');

    s.get('knowledge-sources-status').value = '';
    s.get('knowledge-sources-ext').value = '.xlsx';
    s.ctx.renderKnowledgeSources();
    assert.match(rows(), /报价\.xlsx/);
    assert.doesNotMatch(rows(), /合同\.pdf/);

    s.get('knowledge-sources-ext').value = '';
    s.get('knowledge-sources-category').value = 'legal';
    s.ctx.renderKnowledgeSources();
    assert.match(rows(), /合同\.pdf/);
    assert.doesNotMatch(rows(), /报价\.xlsx/);

    // Filtering everything out is not the same answer as an empty library.
    s.get('knowledge-sources-filter').value = '不存在';
    s.ctx.renderKnowledgeSources();
    assert.equal(s.get('knowledge-sources-no-match').classList.contains('hidden'), false);
    assert.equal(s.get('knowledge-sources-empty').classList.contains('hidden'), true,
        'a library with sources never claims to be empty');
    assert.equal(s.get('knowledge-sources-list').classList.contains('hidden'), true);
    assert.equal(s.requests.length, 0, 'filtering is local, never a round trip');
});

test('a row reports the newest original, the version in effect and its type', async () => {
    const s = jsonResponse(() => LIST);
    s.ctx.loadKnowledgeSources();
    await flush();
    const html = s.get('knowledge-sources-list').innerHTML;
    assert.match(html, /合同\.pdf/);
    // Machine-readable counters: the newest stored original and the version
    // whose body is actually in effect are two different numbers, and v2 can be
    // stored while v1 is still the searchable one.
    assert.match(html, /data-latest-version="2"/);
    assert.match(html, /data-active-version="1"/);
    assert.match(html, /data-status="searchable"/);
    assert.match(html, /原件 v2/, 'the newest stored original is named');
    assert.match(html, /生效 v1/, 'and so is the version in effect');
    assert.match(html, /\.pdf/, 'the type is visible without opening the row');
    assert.match(html, /2026/, 'and so is when it last changed');
});

test('the detail separates the published body from the newest original', async () => {
    const s = setup();
    s.ctx.fetch = async url => ({
        json: async () => (url.includes('/detail')
            ? { ...DETAIL, source: LIST.sources.find(item => url.includes(item.source_id)) }
            : LIST),
    });
    await s.ctx.loadKnowledgeSources();
    await s.ctx.openKnowledgeSourceDetail('src-2');
    await flush();
    const missing = s.get('knowledge-source-detail-body').innerHTML;
    assert.match(missing, /knowledge_sources_body_missing/,
        'a stored-only original says there is no searchable body');
    assert.match(missing, /knowledge_sources_field_active_version/);
    assert.match(missing, /knowledge_sources_field_updated/);
    assert.match(missing, /knowledge_sources_add_version/, 'a new version can be uploaded from here');

    await s.ctx.openKnowledgeSourceDetail('src-1');
    await flush();
    const published = s.get('knowledge-source-detail-body').innerHTML;
    // The published-body line is a template: the version search is using is
    // substituted into the shipped string, so it is resolved here the same way.
    const expected = labelFor('knowledge_sources_body_published').replace('{version}', '1');
    assert.ok(published.includes(expected),
        'and a converted source says which version search is using');
});

test('deleting states the versions and the body it removes', async () => {
    const s = jsonResponse(() => LIST);
    s.ctx.loadKnowledgeSources();
    await flush();
    await s.ctx.setKnowledgeSourceLifecycle('src-1', 'delete');
    assert.equal(s.confirmMessages.length, 1, 'the user is asked exactly once');
    const asked = s.confirmMessages[0];
    assert.match(asked, /2/, 'the number of original versions is stated');
    assert.match(asked, /v1/, 'and the published body search is using');
    assert.doesNotMatch(asked, /\{/, 'the prompt the user agrees to has no unfilled placeholder');

    // A source with nothing published must not claim a published body is going:
    // it names the other branch of the same prompt instead.
    await s.ctx.setKnowledgeSourceLifecycle('src-2', 'delete');
    const second = s.confirmMessages[1];
    assert.doesNotMatch(second, /v1/);
    assert.match(second, /knowledge_sources_delete_body_none/,
        'the honest version is stated instead');
});

test('uploading a new version targets the source and pins the version seen', async () => {
    const s = jsonResponse(() => LIST);
    await s.ctx.loadKnowledgeSources();
    await flush();
    s.ctx.openKnowledgeUploadPanel('src-1');
    assert.match(s.get('knowledge-upload-target').textContent, /合同\.pdf/,
        'the panel says which source is being extended');
    s.ctx.addKnowledgeUploadFiles([{ name: '合同-v3.pdf', size: 10 }]);
    s.ctx.submitKnowledgeUpload();
    const sent = Object.fromEntries(
        s.requests[s.requests.length - 1].body.entries.filter(([key]) => key !== 'files'));
    assert.equal(sent.conflict, 'update', 'a new version never asks the same-name question');
    assert.equal(sent.target_source_id, 'src-1');
    assert.equal(sent.expected_version, '2', 'the version the user was looking at is pinned');
});

test('an empty library can be filled by dropping files on the drop zone', () => {
    const html = fs.readFileSync(CHAT_HTML, 'utf8');
    const empty = html.slice(html.indexOf('id="knowledge-sources-empty"'));
    const zone = empty.slice(0, empty.indexOf('</div>'));
    assert.match(zone, /ondrop=/, 'the empty state is a drop target');
    assert.match(zone, /addKnowledgeUploadFiles\(event\.dataTransfer\.files\)/,
        'a drop lands in the same queue as the picker');
});

test('the knowledge view markup carries every id the sources code addresses', () => {
    const code = sliceSources();
    const ids = addressedIds(code).filter(id => id.startsWith('knowledge-'));
    assert.ok(ids.length > 10, `expected the section to address many ids, saw ${ids.length}`);
    const html = fs.readFileSync(CHAT_HTML, 'utf8');
    const missing = ids.filter(id => !html.includes(`id="${id}"`));
    assert.deepEqual(missing, [], 'every addressed id exists in the assembled page');
});

test('the markup declares the sources tab and upload panel', () => {
    const html = fs.readFileSync(CHAT_HTML, 'utf8');
    for (const id of REQUIRED_IDS) {
        assert.ok(html.includes(`id="${id}"`), `${id} is in the page`);
    }
    assert.match(html, /switchKnowledgeTab\('sources'\)/, 'the tab button switches to it');
    assert.match(html, /data-i18n="knowledge_tab_sources"/, 'and is labelled from the i18n table');
    assert.match(html, /openKnowledgeUploadPanel\(\)/, 'the header button opens the upload panel');
});
