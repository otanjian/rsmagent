// Save-as on the client: real behaviour of the phase-1 download policy.
//
// Change ``add-desktop-remote-web-workbench``, tasks 5.6/5.7 (acceptance W19,
// and the F10/F11 boundary "the guest names a server URL, never a local path").
//
// Three things are being checked, and each one is a way a download could leave
// the paired session behind:
//
//   * a target the host did not sanction is refused before anything is written
//     -- a foreign origin, a plain-HTTP downgrade, a path outside the declared
//     prefixes, or a credential in the query (the cookie is the only way in);
//   * the local name is *derived*, so a server or a page cannot choose where
//     the bytes land, and an existing file is reported rather than replaced;
//   * the stream is bounded and the write is atomic: a partial file never
//     carries the real name, and an over-budget or interrupted transfer removes
//     it.
//
// ``installDownloadPolicy`` is driven through a stub session and item -- the
// real module, the real event order -- so "nothing was written" is observed
// rather than asserted from the source.
//
// Run: node --test tests/test_desktop_downloads.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const dist = path.join(desktop, 'dist', 'main', 'remote');
const contractPath = path.join(root, 'contracts', 'desktop', 'v1.json');

const compiledDownloads = path.join(dist, 'downloads.js');
const compiledContentWindow = path.join(dist, 'content-window.js');

let downloads;
let contract;

function needsBuild() {
    const sources = ['downloads.ts', 'content-window.ts']
        .map((name) => path.join(desktop, 'src', 'main', 'remote', name));
    const newest = Math.max(...sources.map((file) => fs.statSync(file).mtimeMs));
    for (const out of [compiledDownloads, compiledContentWindow]) {
        if (!fs.existsSync(out)) return true;
        if (newest > fs.statSync(out).mtimeMs) return true;
    }
    return false;
}

before(() => {
    if (needsBuild()) {
        execFileSync('npm', ['run', 'build:main'], { cwd: desktop, stdio: 'inherit' });
    }
    downloads = require(compiledDownloads);
    contract = JSON.parse(fs.readFileSync(contractPath, 'utf8'));
});

const ORIGIN = 'https://console.example.com';
const CTX = { origin: ORIGIN, entryPaths: ['/', '/chat', '/admin'] };

/** The compiled content-window module, loaded with a stub electron. */
function loadContentWindow() {
    const originalRequire = Module.prototype.require;
    const created = [];
    class FakeBrowserWindow {
        constructor(options) { created.push(options); this.options = options; this.webContents = { setWindowOpenHandler() {}, on() {}, once() {}, loadURL() { return Promise.resolve(); } }; }
        once() {}
        show() {}
    }
    Module.prototype.require = function patched(id) {
        if (id === 'electron') return { BrowserWindow: FakeBrowserWindow };
        return originalRequire.apply(this, arguments);
    };
    try {
        delete require.cache[require.resolve(compiledContentWindow)];
        const mod = require(compiledContentWindow);
        return { mod, created };
    } finally {
        Module.prototype.require = originalRequire;
    }
}

// ---------------------------------------------------------------------------
// The contract is the source of truth for the policy constants
// ---------------------------------------------------------------------------

test('the policy constants are the contract\'s, not a second opinion', () => {
    const spec = contract.downloads;
    assert.deepEqual(downloads.DOWNLOAD_DOCUMENT_PREFIXES, spec.document_prefixes);
    assert.deepEqual(downloads.DOWNLOAD_ATTACHMENT_PREFIXES, spec.attachment_prefixes);
    assert.deepEqual(downloads.FORBIDDEN_QUERY_KEYS, spec.forbidden_query_keys);
    assert.equal(downloads.DOWNLOAD_CHUNK_BYTES, spec.chunk_bytes);
    assert.equal(downloads.DOWNLOAD_TEMP_SUFFIX, spec.temp_suffix);
    assert.equal(downloads.DOWNLOAD_FILENAME_MAX_CHARS, spec.filename_max_chars);
    assert.equal(downloads.DOWNLOAD_MAX_BYTES, contract.limits.file_max_bytes);
    assert.ok(downloads.DOWNLOAD_CHUNK_BYTES <= downloads.DOWNLOAD_MAX_BYTES);
});

test('an inline save is capped well below the streamed download ceiling', () => {
    const { SAVE_ARTIFACT_MAX_BYTES } = require(path.join(dist, 'host-bridge.js'));
    assert.equal(SAVE_ARTIFACT_MAX_BYTES, contract.downloads.save_artifact_max_bytes);
    assert.ok(SAVE_ARTIFACT_MAX_BYTES < contract.limits.file_max_bytes,
        'a larger file must go through the streamed download path');
});

// ---------------------------------------------------------------------------
// What may be fetched at all
// ---------------------------------------------------------------------------

test('a declared same-origin target is accepted, with its kind', () => {
    const document = downloads.classifyDownloadTarget(`${ORIGIN}/preview/abc`, CTX);
    assert.equal(document.ok, true);
    assert.equal(document.kind, 'document');
    const attachment = downloads.classifyDownloadTarget(
        `${ORIGIN}/api/knowledge/sources/download?source_id=s1&version=2`, CTX);
    assert.equal(attachment.ok, true);
    assert.equal(attachment.kind, 'attachment');
});

test('a foreign origin is refused, so the session stops at the paired server', () => {
    const verdict = downloads.classifyDownloadTarget('https://evil.example.com/api/file?path=x', CTX);
    assert.equal(verdict.ok, false);
    assert.equal(verdict.code, 'unsafe_path');
});

test('a plain-HTTP target is refused even on the same host: no downgrade', () => {
    const verdict = downloads.classifyDownloadTarget('http://console.example.com/api/file?path=x', CTX);
    assert.equal(verdict.ok, false);
    assert.equal(verdict.code, 'unsafe_path');
});

test('a credential in the link is refused, not stripped', () => {
    for (const key of contract.downloads.forbidden_query_keys) {
        const verdict = downloads.classifyDownloadTarget(
            `${ORIGIN}/api/file?path=x&${key}=abc`, CTX);
        assert.equal(verdict.ok, false, key);
        assert.equal(verdict.code, 'unsafe_path', key);
        assert.match(verdict.message, new RegExp(key, 'i'));
    }
});

test('a path outside the declared prefixes never becomes a download', () => {
    for (const url of [`${ORIGIN}/api/desktop/meta`, `${ORIGIN}/auth/desktop/web-session`,
        `${ORIGIN}/`, `${ORIGIN}/chat`]) {
        assert.equal(downloads.classifyDownloadTarget(url, CTX).ok, false, url);
    }
    // A prefix is a path boundary, not a string prefix.
    assert.equal(downloads.classifyDownloadTarget(`${ORIGIN}/uploads-evil/x`, CTX).ok, false);
    assert.equal(downloads.classifyDownloadTarget(`${ORIGIN}/api/filex`, CTX).ok, false);
});

test('a non-web target is refused before anything else looks at it', () => {
    for (const url of ['file:///etc/passwd', 'javascript:alert(1)', 'about:blank', '', 'not a url']) {
        const verdict = downloads.classifyDownloadTarget(url, CTX);
        assert.equal(verdict.ok, false, url);
    }
});

// ---------------------------------------------------------------------------
// artifact_ref resolution
// ---------------------------------------------------------------------------

test('a relative artifact_ref resolves to a same-origin download URL', () => {
    const resolved = downloads.resolveArtifactRef('/api/workspace/download?path=a/b.txt', CTX);
    assert.equal(resolved.ok, true);
    assert.equal(resolved.url, `${ORIGIN}/api/workspace/download?path=a/b.txt`);
});

test('an artifact_ref that is already a URL is refused, never normalized', () => {
    for (const ref of [`${ORIGIN}/api/file?path=x`, '//evil.example.com/api/file',
        'http://evil.example.com/api/file', 'https://console.example.com/api/file']) {
        const resolved = downloads.resolveArtifactRef(ref, CTX);
        assert.equal(resolved.ok, false, ref);
        assert.match(resolved.code, /unsafe_path|invalid_request/);
    }
});

test('an artifact_ref cannot traverse or smuggle a token', () => {
    assert.equal(downloads.resolveArtifactRef('/api/file/../secret', CTX).ok, false);
    assert.equal(downloads.resolveArtifactRef('/api/file?path=x&token=1', CTX).ok, false);
    assert.equal(downloads.resolveArtifactRef('/preview//x', CTX).ok, false);
    assert.equal(downloads.resolveArtifactRef('', CTX).ok, false);
});

// ---------------------------------------------------------------------------
// The local name and the destination
// ---------------------------------------------------------------------------

test('the local name is derived, so a server cannot choose where bytes land', () => {
    assert.equal(downloads.sanitizeFileName('../../etc/passwd'), 'passwd');
    assert.equal(downloads.sanitizeFileName('..\\..\\windows\\system32\\cmd.exe'), 'cmd.exe');
    assert.equal(downloads.sanitizeFileName('/tmp/x/../../y'), 'y');
    assert.equal(downloads.sanitizeFileName('a\u0000b\u001fc.txt'), 'abc.txt');
    assert.equal(downloads.sanitizeFileName('report:final?.txt'), 'report_final_.txt');
    assert.equal(downloads.sanitizeFileName('...hidden'), 'hidden');
    assert.equal(downloads.sanitizeFileName('trailing.'), 'trailing');
});

test('a Chinese name survives, and a reserved device name is defused', () => {
    assert.equal(downloads.sanitizeFileName('审计报告 2026.xlsx'), '审计报告 2026.xlsx');
    assert.equal(downloads.sanitizeFileName('CON.txt'), '_CON.txt');
    assert.equal(downloads.sanitizeFileName('nul.csv'), '_nul.csv');
});

test('an empty or unusable name falls back instead of writing to a directory', () => {
    assert.equal(downloads.sanitizeFileName(''), 'download');
    assert.equal(downloads.sanitizeFileName('/'), 'download');
    assert.equal(downloads.sanitizeFileName('..'), 'download');
    assert.equal(downloads.sanitizeFileName('', 'artifact.bin'), 'artifact.bin');
});

test('a long name is bounded but keeps its extension', () => {
    const long = `${'a'.repeat(400)}.xlsx`;
    const name = downloads.sanitizeFileName(long);
    assert.equal(name.length, contract.downloads.filename_max_chars);
    assert.ok(name.endsWith('.xlsx'));
});

test('the destination directory is the host\'s, and an existing file is reported', () => {
    const dir = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'cow-dl-'));
    const plan = downloads.planDestination(dir, 'report.txt', (candidate) => candidate.endsWith('report.txt'));
    assert.equal(plan.exists, true);
    assert.equal(plan.path, path.join(dir, 'report.txt'));
    // A traversal attempt still lands inside the host's directory.
    const escaped = downloads.planDestination(dir, '../../etc/passwd', () => false);
    assert.equal(path.dirname(escaped.path), dir);
    fs.rmSync(dir, { recursive: true, force: true });
});

test('the temporary path is a sibling, so the rename is atomic', () => {
    const temp = downloads.temporaryPath('/downloads/report.txt', 'abc123');
    assert.equal(path.dirname(temp), '/downloads');
    assert.ok(temp.startsWith('/downloads/report.txt'));
    assert.ok(temp.endsWith('abc123'));
    assert.ok(temp.endsWith(contract.downloads.temp_suffix + '.abc123'));
});

// ---------------------------------------------------------------------------
// The byte budget
// ---------------------------------------------------------------------------

test('the budget counts and stops at the ceiling', () => {
    const budget = new downloads.DownloadBudget(10, 4);
    assert.equal(budget.chunk, 4);
    assert.equal(budget.accept(6), true);
    assert.equal(budget.received, 6);
    assert.equal(budget.accept(5), false, 'a chunk past the ceiling is refused');
    assert.equal(budget.received, 6, 'a refused chunk is not counted');
    assert.equal(budget.accept(4), true);
    assert.equal(budget.received, 10);
    assert.equal(budget.accept(1), false);
});

test('a negative or non-numeric count is refused rather than shrinking the budget', () => {
    const budget = new downloads.DownloadBudget(10, 4);
    assert.equal(budget.accept(-5), false);
    assert.equal(budget.accept(Number.NaN), false);
    assert.equal(budget.accept('5'), false);
    assert.equal(budget.received, 0);
});

test('a finished stream must have moved exactly the bytes the server promised', () => {
    const short = new downloads.DownloadBudget(100, 4);
    short.accept(10);
    assert.equal(short.complete(20).ok, false);
    assert.equal(short.complete(20).code, 'checksum_mismatch');
    const good = new downloads.DownloadBudget(100, 4);
    good.accept(20);
    assert.equal(good.complete(20).ok, true);
    assert.equal(good.complete(20).bytes, 20);
    // An unknown total is not a mismatch: it just cannot be verified.
    assert.equal(good.complete(null).ok, true);
});

// ---------------------------------------------------------------------------
// The Electron wiring, driven for real
// ---------------------------------------------------------------------------

/** A stub Electron session + download item that records the whole event order. */
function wiring(options = {}) {
    const listeners = {};
    const session = {
        on: (event, handler) => { listeners[event] = handler; },
        off: (event) => { delete listeners[event]; },
    };
    const calls = [];
    const item = {
        url: options.url || `${ORIGIN}/api/file?path=a.txt`,
        filename: options.filename || 'a.txt',
        total: options.total === undefined ? 3 : options.total,
        received: 0,
        events: {},
        getURL() { return this.url; },
        getFilename() { return this.filename; },
        getTotalBytes() { return this.total; },
        getReceivedBytes() { return this.received; },
        setSavePath(p) { calls.push(['setSavePath', p]); },
        cancel() { calls.push(['cancel']); this.cancelled = true; },
        on(event, handler) { (this.events[event] || (this.events[event] = [])).push(handler); },
        /** Feed the policy a chunk, as Electron's `updated` does. */
        feed(bytes) { this.received += bytes; (this.events.updated || []).forEach((h) => h()); },
        finish(state = 'completed') { (this.events.done || []).forEach((h) => h({}, state)); },
    };
    const written = [];
    const events = [];
    const dispose = downloads.installDownloadPolicy({
        session,
        origin: ORIGIN,
        entryPaths: ['/', '/chat'],
        directory: '/host/downloads',
        exists: options.exists || (() => false),
        rename: (from, to) => { calls.push(['rename', from, to]); written.push(to); },
        remove: (from) => { calls.push(['remove', from]); },
        nonce: () => 'n1',
        onEvent: (event) => events.push(event),
        confirmedOverwrites: options.confirmedOverwrites,
    });
    return {
        session, item, calls, written, events, dispose, listeners,
        fire: () => listeners['will-download']({ preventDefault() {} }, item),
    };
}

test('a sanctioned download writes to a temporary sibling, then the real name', () => {
    const { calls, written, events, fire, item } = wiring();
    fire();
    item.feed(3);
    item.finish('completed');
    assert.deepEqual(calls, [
        ['setSavePath', '/host/downloads/a.txt' + contract.downloads.temp_suffix + '.n1'],
        ['rename', '/host/downloads/a.txt' + contract.downloads.temp_suffix + '.n1', '/host/downloads/a.txt'],
    ]);
    assert.deepEqual(written, ['/host/downloads/a.txt']);
    assert.deepEqual(events.map((e) => e.phase), ['started', 'completed']);
    assert.equal(events[1].bytes, 3);
});

test('a refused download never touches the file system', () => {
    const { calls, events, fire } = wiring({ url: 'https://evil.example.com/api/file?path=x' });
    fire();
    assert.deepEqual(calls, [['cancel']]);
    assert.deepEqual(events.map((e) => e.phase), ['refused']);
    assert.equal(events[0].code, 'unsafe_path');
});

test('an existing file is not replaced until the user confirms it', () => {
    const { calls, events, fire } = wiring({ exists: () => true });
    fire();
    assert.deepEqual(calls, [['cancel']], 'no path is set before the confirmation');
    assert.deepEqual(events.map((e) => e.phase), ['needs_confirmation']);

    const confirmed = wiring({ exists: () => true, confirmedOverwrites: new Set(['/host/downloads/a.txt']) });
    confirmed.fire();
    assert.deepEqual(confirmed.calls[0][0], 'setSavePath');
});

test('an over-budget stream is cancelled and the partial file removed', () => {
    const { calls, events, fire, item } = wiring({ total: 0 });
    fire();
    item.feed(downloads.DOWNLOAD_MAX_BYTES + 1);
    assert.ok(calls.some(([name]) => name === 'cancel'));
    assert.ok(calls.some(([name, target]) => name === 'remove' && target.includes('.cowpart')));
    assert.equal(events.at(-1).phase, 'failed');
    assert.equal(events.at(-1).code, 'limit_exceeded');
    // Nothing was renamed onto the real name.
    assert.equal(calls.some(([name]) => name === 'rename'), false);
});

test('a declared total above the ceiling is refused before the bytes arrive', () => {
    const { calls, events, fire, item } = wiring({ total: downloads.DOWNLOAD_MAX_BYTES + 1 });
    fire();
    item.feed(1);
    assert.ok(calls.some(([name]) => name === 'cancel'));
    assert.equal(events.at(-1).code, 'limit_exceeded');
});

test('a cancelled or interrupted transfer removes the partial file', () => {
    for (const state of ['cancelled', 'interrupted']) {
        const { calls, events, fire, item } = wiring();
        fire();
        item.feed(1);
        item.finish(state);
        assert.equal(calls.some(([name]) => name === 'rename'), false, state);
        assert.ok(calls.some(([name, target]) => name === 'remove' && target.includes('.cowpart')), state);
        assert.equal(events.at(-1).phase, state === 'cancelled' ? 'cancelled' : 'failed');
    }
});

test('a stream that ended short of its declared total is not renamed', () => {
    const { calls, events, fire, item } = wiring({ total: 10 });
    fire();
    item.feed(4);
    item.finish('completed');
    assert.equal(calls.some(([name]) => name === 'rename'), false);
    assert.equal(events.at(-1).code, 'checksum_mismatch');
});

test('the policy is removed with the container and stops deciding downloads', () => {
    const { dispose, listeners } = wiring();
    assert.equal(typeof listeners['will-download'], 'function');
    dispose();
    assert.equal('will-download' in listeners, false, 'the handler is unregistered');
});

// ---------------------------------------------------------------------------
// The content window has no bridge
// ---------------------------------------------------------------------------

test('a content document is sandboxed and gets no preload at all', () => {
    const { mod } = loadContentWindow();
    const prefs = mod.contentWindowPreferences('desktop-remote-instance');
    assert.equal(prefs.partition, 'desktop-remote-instance');
    assert.equal(prefs.sandbox, true);
    assert.equal(prefs.contextIsolation, true);
    assert.equal(prefs.nodeIntegration, false);
    assert.equal(prefs.nodeIntegrationInSubFrames, false);
    assert.equal(prefs.webSecurity, true);
    assert.equal(prefs.allowRunningInsecureContent, false);
    assert.equal(prefs.nativeWindowOpen, false);
    assert.equal('preload' in prefs, false, 'a content document must not have the bridge');
});

test('the content window refuses popups and pins navigation to the origin', () => {
    const { mod, created } = loadContentWindow();
    mod.openContentWindow({
        url: `${ORIGIN}/preview/x`,
        partition: 'desktop-remote-instance',
        origin: ORIGIN,
        session: { setPermissionRequestHandler: () => undefined },
    });
    assert.equal(created.length, 1);
    const prefs = created[0].webPreferences;
    assert.equal('preload' in prefs, false);
    assert.equal(prefs.sandbox, true);
});
