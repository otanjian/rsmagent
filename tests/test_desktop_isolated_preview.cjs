// The isolated preview of a local project file (change
// align-desktop-project-execution-with-master, task 9.5, acceptance A25).
//
// A local file has no URL, and the two obvious ways to give it one are both
// wrong: a server route would answer about a *different* file with the same name,
// and a `file://` path would publish a permanent address for the user's disk to
// the one document that holds the bridge. What the panel gets instead is a URL
// that authorizes exactly one file, stops working on a clock, and renders under a
// policy the page cannot relax.
//
// Three claims are tested, and they are tested where they can actually fail:
//
//   * the *decision* (`planLocalPreview`, the compiled main-process module):
//     which files have a preview, and which are refused by name -- a directory,
//     a link, a file too large, a revoked grant, a kind this surface cannot
//     render;
//   * the *resource* (`createPreviewTickets` / `previewResourceFor` /
//     `answerPreviewRequest`): one ticket is one file on a clock, an expired or
//     unknown ticket answers 404 with none of the file's bytes, and every answer
//     carries the restrictive policy;
//   * the *surfaces a page can see*: the bridge method's shape and refusals, the
//     response's CSP as the browser would get it, and the page code that embeds
//     the URL instead of the bytes.
//
// The Electron-level claims -- that the scheme is embeddable from a real page,
// that a preview frame has no bridge and no network, that a page cannot `fetch()`
// the bytes, and that an expired URL really dies -- are measured by
// `desktop/e2e/preview-isolation.probe.cjs`, whose output is the evidence for
// this task. This file is the part that must hold on every run.
//
// Run: node --test tests/test_desktop_isolated_preview.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');

const ROOT = path.join(__dirname, '..');
const DESKTOP = path.join(ROOT, 'desktop');
const DIST = path.join(DESKTOP, 'dist', 'main');
const SRC = path.join(DESKTOP, 'src', 'main');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');
const workspaceJs = read('channel/web/static/js/workspace.js');
const projectSourceJs = read('channel/web/static/js/fork/project-source.js');
const hostAdapterJs = read('channel/web/static/js/fork/desktop-host.js');
const coreI18nJs = read('channel/web/static/js/i18n/core.js');
const containerTs = read('desktop/src/main/remote/remote-container-ipc.ts');
const bridgeTs = read('desktop/src/main/remote/host-bridge.ts');
const preloadTs = read('desktop/src/main/remote-preload.ts');

const compiled = {
  preview: path.join(DIST, 'project-browser', 'preview.js'),
  native: path.join(DIST, 'project-browser', 'native-actions.js'),
  hostIpc: path.join(DIST, 'remote', 'remote-host-ipc.js'),
  localFiles: path.join(DIST, 'remote', 'local-files-bridge.js'),
};

function needsBuild() {
  const newest = Math.max(
    ...[
      'project-browser/preview.ts', 'project-browser/native-actions.ts',
      'remote/host-bridge.ts', 'remote/remote-host-ipc.ts',
      'remote/local-files-bridge.ts',
    ].map((rel) => fs.statSync(path.join(SRC, rel)).mtimeMs),
  );
  return Object.values(compiled).some(
    (out) => !fs.existsSync(out) || fs.statSync(out).mtimeMs < newest,
  );
}

let preview;
before(() => {
  if (needsBuild()) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: DESKTOP, stdio: 'pipe' });
  }
  preview = require(compiled.preview);
  assert.equal(typeof preview.planLocalPreview, 'function', 'the planner is compiled');
  assert.equal(typeof preview.answerPreviewRequest, 'function', 'the scheme answer is compiled');
});

// ---------------------------------------------------------------------------
// A real project on a real disk, and the entry the panel resolves
// ---------------------------------------------------------------------------

/** A real project directory, already disambiguated from /tmp's own links. */
function makeProject() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-preview-')));
  fs.mkdirSync(path.join(dir, 'out'));
  fs.writeFileSync(path.join(dir, 'report.html'), '<html><body><h1>hi</h1></body></html>', 'utf8');
  fs.writeFileSync(path.join(dir, 'out', 'chart.png'), Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  fs.writeFileSync(path.join(dir, 'notes.txt'), 'plain', 'utf8');
  fs.writeFileSync(path.join(dir, 'sheet.xlsx'), 'PK\u0003\u0004', 'utf8');
  return dir;
}

/** The entry `ProjectBrowser.resolve` answers for a path, as the port passes it. */
function entryFor(root, rel, overrides = {}) {
  const full = path.join(root, ...rel.split('/'));
  const stat = fs.lstatSync(full);
  return {
    ok: true,
    type: 'entry',
    path: rel,
    kind: stat.isDirectory() ? 'dir' : 'file',
    size: stat.isDirectory() ? null : stat.size,
    modified: Math.trunc(stat.mtimeMs / 1000),
    grant_version: 1,
    ...overrides,
  };
}

/** Plan one preview against a real project, the way the container port does. */
function plan(root, rel, options = {}) {
  const { entry = {}, ...rest } = options;
  return preview.planLocalPreview({
    path: rel,
    root,
    entry: entry.ok === false ? entry : entryFor(root, rel, entry),
    ...rest,
  });
}

// ---------------------------------------------------------------------------
// The decision: what may be previewed, and why not
// ---------------------------------------------------------------------------

test('a generated page and an image are previewable, with their kind and size', (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  const page = plan(dir, 'report.html');
  assert.equal(page.ok, true);
  assert.equal(page.kind, 'html');
  assert.equal(page.name, 'report.html');
  assert.equal(page.size, fs.statSync(path.join(dir, 'report.html')).size);
  // The absolute path is main-process only: the ticket is minted for it and it
  // is never part of what the page is told (asserted again on the reply below).
  assert.equal(page.absolutePath, path.join(dir, 'report.html'));

  assert.equal(plan(dir, 'out/chart.png').kind, 'image');
  assert.equal(page.url, undefined, 'a plan is not a URL: the ticket comes later');
});

test('the kind follows the file, not the page: svg is markup, xlsx is not', (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(dir, 'logo.svg'), '<svg xmlns="http://www.w3.org/2000/svg"/>', 'utf8');
  fs.writeFileSync(path.join(dir, 'clip.mp4'), 'not really', 'utf8');

  // An SVG document can carry script, so it is served as markup (and sandboxed)
  // rather than as a picture.
  assert.equal(preview.previewKindOf('logo.svg'), 'html');
  assert.equal(preview.previewKindOf('CLIP.MP4'), 'media', 'case is not the extension');
  assert.equal(preview.previewKindOf('sheet.xlsx'), '');
  assert.equal(plan(dir, 'logo.svg').kind, 'html');
});

test('a kind with no isolated preview is refused by name, not rendered blank', (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  for (const rel of ['notes.txt', 'sheet.xlsx']) {
    const refused = plan(dir, rel);
    assert.equal(refused.ok, false, rel);
    assert.equal(refused.code, 'unsupported_type', rel);
    // The refusal has to say what to do instead: this shell has no PDF viewer
    // and no text renderer for a *file* preview, and the system does.
    assert.match(refused.message, /system application/);
  }
  // A spreadsheet explicitly: A25's "用系统应用打开表格" is the answer for it.
  assert.equal(plan(dir, 'sheet.xlsx').code, 'unsupported_type');
});

test('a file too large for the isolated surface is refused, not truncated', (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(dir, 'huge.html'), 'x'.repeat(64), 'utf8');

  const refused = plan(dir, 'huge.html', { maxBytes: 16 });
  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'limit_exceeded');
  assert.match(refused.message, /16 byte preview limit/);

  // Exactly at the bound is allowed: the bound is a size, not a margin.
  assert.equal(plan(dir, 'huge.html', { maxBytes: 64 }).ok, true);
});

test('a directory is not a document, and neither is a link out of the project', (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  const directory = plan(dir, 'out');
  assert.equal(directory.ok, false);
  assert.equal(directory.code, 'not_a_file');

  // A link is refused by the same check the system actions use -- a preview must
  // not become the way to read through a link the read path would never follow.
  fs.symlinkSync(path.join(dir, 'notes.txt'), path.join(dir, 'out', 'link.html'));
  const link = plan(dir, 'out/link.html');
  assert.equal(link.ok, false);
  assert.equal(link.code, 'not_a_file');
  assert.match(link.message, /link/);

  // A directory *component* that is really a link out of the project is refused
  // on the resolved path, not on the joined string: `out/chart.png` looks like a
  // project file and is not one.
  const outside = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-outside-')));
  t.after(() => fs.rmSync(outside, { recursive: true, force: true }));
  fs.writeFileSync(path.join(outside, 'chart.png'), 'png', 'utf8');
  fs.rmSync(path.join(dir, 'out'), { recursive: true, force: true });
  fs.symlinkSync(outside, path.join(dir, 'out'));
  const escaped = plan(dir, 'out/chart.png');
  assert.equal(escaped.ok, false);
  assert.equal(escaped.code, 'unsafe_path');
});

test('the helper\'s own refusal travels with the helper\'s own code', (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  // "the file is gone" and "the project is closed" are different sentences for
  // the user, so the code is passed through rather than flattened into one.
  const gone = plan(dir, 'report.html', { entry: { ok: false, code: 'not_found', message: 'no such file' } });
  assert.equal(gone.ok, false);
  assert.equal(gone.code, 'not_found');
  assert.equal(gone.message, 'no such file');

  const offline = plan(dir, 'report.html', { entry: { ok: false, code: 'device_offline' } });
  assert.equal(offline.code, 'device_offline');
  assert.match(offline.message, /not available on this machine/,
    'a refusal with no message of its own still says something true');

  // A refusal that carries no code at all is still a refusal.
  assert.equal(plan(dir, 'report.html', { entry: { ok: false } }).code, 'not_found');
});

test('no live grant root is a revoked project, and nothing is previewed from it', (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  const entry = entryFor(dir, 'report.html');
  // `root: null` is what `remoteGrantRegistry.absolutePathFor` answers for a
  // grant that was replaced since the panel's confirmation.
  const refused = preview.planLocalPreview({ path: 'report.html', root: null, entry });
  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'stale_context');

  // A root that is gone from disk is the same answer: the project is no longer
  // there to be previewed from.
  const vanished = preview.planLocalPreview({ path: 'report.html', root: path.join(dir, 'gone'), entry });
  assert.equal(vanished.ok, false);
  assert.equal(vanished.code, 'stale_context');
});

// ---------------------------------------------------------------------------
// The bytes: bounded, positional, and refused rather than shortened
// ---------------------------------------------------------------------------

test('a preview reads at most its bound, and refuses a byte more', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const big = path.join(dir, 'big.html');
  fs.writeFileSync(big, Buffer.alloc(4096, 0x61)); // 4 KiB of 'a'

  // The real reader, against a real file: one byte over the bound comes back --
  // that is *how* the caller learns the file did not fit. Four kilobytes did not
  // have to be carried to find that out.
  const peeked = await preview.defaultPreviewReader(big, 1024);
  assert.equal(peeked.byteLength, 1025);

  const refused = await preview.readLocalPreviewContent({ absolutePath: big, maxBytes: 1024 });
  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'limit_exceeded');

  // Within the bound, the whole file: a preview that truncated would render half
  // a report as if it were whole.
  const whole = await preview.readLocalPreviewContent({ absolutePath: big, maxBytes: 4096 });
  assert.equal(whole.ok, true);
  assert.equal(whole.content.byteLength, 4096);

  // The bound is handed *to the reader*, so an implementation that ignores it is
  // visible rather than silently trusted.
  const seen = [];
  await preview.readLocalPreviewContent({
    absolutePath: big,
    maxBytes: 32,
    read: async (p, limit) => { seen.push(limit); return Buffer.alloc(limit + 1); },
  });
  assert.deepEqual(seen, [32], 'the reader is told the bound it must respect');
});

// ---------------------------------------------------------------------------
// The ticket: one file, one URL, on a clock
// ---------------------------------------------------------------------------

/** A store whose clock and tokens the test owns. */
function makeTickets(options = {}) {
  let now = 1000;
  let counter = 0;
  const store = preview.createPreviewTickets({
    ttlMs: options.ttlMs === undefined ? 120000 : options.ttlMs,
    max: options.max,
    now: () => now,
    random: (bytes) => {
      counter += 1;
      return (options.token || 'a'.repeat(bytes - 2)) + String(counter).padStart(2, '0');
    },
  });
  return { store, advance: (ms) => { now += ms; }, tokens: () => counter };
}

test('a ticket names one file, and the URL carries no path', () => {
  const { store } = makeTickets();
  const ticket = store.issue({ content: Buffer.from('<html></html>'), name: 'report.html', kind: 'html' });

  assert.equal(ticket.name, 'report.html');
  assert.equal(ticket.kind, 'html');
  assert.equal(ticket.size, 13);
  assert.equal(ticket.expiresAt, 1000 + 120000);
  assert.equal(ticket.url, `cow-preview://preview/${ticket.token}/report.html`);
  assert.equal(ticket.url.includes('/Users/'), false);
  assert.equal(ticket.url.includes(',/'), false, 'the URL names no directory');
  assert.equal(store.live(), 1);
});

test('the token is not derivable from the name, and two previews of one file differ', () => {
  const { store } = makeTickets();
  const first = store.issue({ content: 'a', name: 'report.html', kind: 'html' });
  const second = store.issue({ content: 'b', name: 'report.html', kind: 'html' });
  assert.notEqual(first.token, second.token, 'a guessable URL is a permanent URL');
  assert.notEqual(first.url, second.url);
  assert.equal(preview.previewUrlFor('tok-en', 'a b.html'), 'cow-preview://preview/tok-en/a%20b.html');
});

test('a ticket stops answering when it expires, and can be revoked sooner', () => {
  const { store, advance } = makeTickets({ ttlMs: 60000 });
  const ticket = store.issue({ content: '<h1>secret</h1>', name: 'report.html', kind: 'html' });

  assert.ok(store.contentFor(ticket.token));
  advance(59999);
  assert.ok(store.contentFor(ticket.token), 'still inside the window');
  advance(1);
  assert.equal(store.contentFor(ticket.token), null, 'the clock is what ends it');

  const revocable = store.issue({ content: 'x', name: 'a.html', kind: 'html' });
  assert.ok(store.contentFor(revocable.token));
  store.revoke(revocable.token);
  assert.equal(store.contentFor(revocable.token), null);

  const all = store.issue({ content: 'x', name: 'b.html', kind: 'html' });
  store.revokeAll();
  assert.equal(store.contentFor(all.token), null);
  assert.equal(store.live(), 0);
});

test('the store is bounded, and the oldest preview is the one that goes', () => {
  const { store } = makeTickets({ max: 3, ttlMs: 60000 });
  const issued = ['a.html', 'b.html', 'c.html', 'd.html'].map((name) =>
    store.issue({ content: name, name, kind: 'html' }));
  assert.equal(store.live(), 3);
  assert.equal(store.contentFor(issued[0].token), null, 'the first ticket is evicted');
  assert.ok(store.contentFor(issued[3].token), 'the newest preview still works');
});

// ---------------------------------------------------------------------------
// The resource: what one request may be answered with
// ---------------------------------------------------------------------------

test('a live ticket answers with exactly its own file, under the kind\'s policy', () => {
  const { store } = makeTickets();
  const ticket = store.issue({ content: '<h1>hello</h1>', name: 'report.html', kind: 'html' });

  const answer = preview.answerPreviewRequest(ticket.url, store);
  assert.equal(answer.status, 200);
  assert.equal(answer.body.toString('utf8'), '<h1>hello</h1>');
  assert.equal(answer.headers['Content-Type'], 'text/html; charset=utf-8');
  assert.match(answer.headers['Content-Security-Policy'], /sandbox allow-scripts/);
  assert.match(answer.headers['Cache-Control'], /no-store/);
  assert.equal(answer.headers['X-Content-Type-Options'], 'nosniff');
  assert.equal(answer.headers['Referrer-Policy'], 'no-referrer');
});

test('an unknown, expired or revoked ticket is a 404 with none of the bytes', () => {
  const { store, advance } = makeTickets({ ttlMs: 1000 });
  const ticket = store.issue({ content: '<h1>secret</h1>', name: 'report.html', kind: 'html' });
  advance(1000);

  for (const url of [
    `cow-preview://preview/${ticket.token}/report.html`,
    'cow-preview://preview/nosuchtoken/report.html',
    'cow-preview://preview//report.html',
  ]) {
    const answer = preview.answerPreviewRequest(url, store);
    assert.equal(answer.status, 404, url);
    assert.equal(answer.body.toString('utf8').includes('secret'), false, url);
    assert.match(answer.headers['Content-Security-Policy'], /sandbox/);
  }
});

test('one ticket cannot be spent on another name, another scheme or another host', () => {
  const { store } = makeTickets();
  const ticket = store.issue({ content: 'png-bytes', name: 'chart.png', kind: 'image' });

  const wrong = [
    `cow-preview://preview/${ticket.token}/other.png`,
    `cow-preview://preview/${ticket.token}/`,
    `cow-preview://preview/${ticket.token}/nested/chart.png`,
    `cow-preview://evil/${ticket.token}/chart.png`,
    `file://${ticket.token}/chart.png`,
    `https://console.example.com/${ticket.token}/chart.png`,
    'not a url at all',
  ];
  for (const url of wrong) {
    const answer = preview.answerPreviewRequest(url, store);
    assert.equal(answer.status, 404, url);
    assert.equal(answer.body.toString('utf8').includes('png-bytes'), false, url);
  }
  // The one that does match: encoding in the path is tolerated, because the name
  // is decoration and Chromium may re-encode it on the way to the handler.
  assert.equal(preview.previewResourceFor(ticket.url, store).ok, true);
  const encoded = `cow-preview://preview/${ticket.token}/chart%2Epng`;
  assert.equal(preview.previewResourceFor(encoded, store).ok, true);
});

test('every preview policy is restrictive: no same-origin, no network, no scripts for data', () => {
  const html = preview.previewContentSecurityPolicy('html');
  assert.match(html, /default-src 'none'/);
  assert.match(html, /connect-src 'none'/);
  assert.match(html, /frame-src 'none'/);
  assert.match(html, /object-src 'none'/);
  assert.match(html, /form-action 'none'/);
  assert.match(html, /base-uri 'none'/);
  assert.match(html, /sandbox allow-scripts allow-forms allow-modals/);
  // The one directive whose absence is the whole isolation story: without
  // `allow-same-origin` the document is an opaque origin, so it cannot read the
  // console's storage, its cookie, or its DOM.
  assert.equal(/allow-same-origin/.test(html), false, 'a same-origin preview is not isolated');
  assert.equal(/allow-popups/.test(html), false, 'a preview is not a place to launch things');

  for (const kind of ['image', 'media']) {
    const policy = preview.previewContentSecurityPolicy(kind);
    assert.equal(policy.endsWith('; sandbox'), true, kind);
    assert.equal(/allow-scripts/.test(policy), false, `${kind} needs no script`);
    assert.match(policy, /default-src 'none'/);
  }
});

// ---------------------------------------------------------------------------
// The bridge: what a page may ask for, and what it is told
// ---------------------------------------------------------------------------

/** Load the compiled bridge with a stub `electron`, as the container sees it. */
function loadWithStub(compiledPath, handlers) {
  const originalRequire = Module.prototype.require;
  const fakeElectron = {
    app: { isPackaged: false, getVersion: () => '0.0.1', getPath: () => '/tmp', on: () => undefined },
    ipcMain: {
      handle: (channel, fn) => { handlers[channel] = fn; },
      removeHandler: (channel) => { delete handlers[channel]; },
      on: () => undefined,
    },
    shell: { openExternal: () => Promise.resolve() },
    dialog: { showSaveDialog: async () => ({ canceled: true }) },
    BrowserWindow: { fromWebContents: () => null },
    session: { fromPartition: () => ({}) },
  };
  Module.prototype.require = function patched(id) {
    if (id === 'electron') return fakeElectron;
    return originalRequire.apply(this, arguments);
  };
  try {
    delete require.cache[require.resolve(compiledPath)];
    return require(compiledPath);
  } finally {
    Module.prototype.require = originalRequire;
  }
}

/** The bridge as the page sees it, with a projects port the test controls. */
function makeBridge(port) {
  const handlers = {};
  const ipc = loadWithStub(compiled.hostIpc, handlers);
  ipc.registerRemoteHost({
    webContents: { id: 7, send: () => undefined },
    context: () => ({
      webContents: { webContentsId: 7, frameProcessId: 100, frameRoutingId: 3 },
      shellFrame: { webContentsId: 7, frameProcessId: 100, frameRoutingId: 3 },
      origin: 'https://console.example.com',
      generation: 4,
      entryPaths: ['/chat'],
    }),
    emit: () => undefined,
    ...(port ? { projects: port } : {}),
  });
  ipc.setupRemoteHostIPC();
  const call = (method, params) => handlers['desktop:bridge:call'](
    {
      sender: { id: 7, mainFrame: { processId: 100, routingId: 3 } },
      senderFrame: { processId: 100, routingId: 3, url: 'https://console.example.com/chat' },
    },
    { method, params: params || {}, generation: 4 },
  );
  const localFiles = require(compiled.localFiles);
  localFiles.setRemoteLocalFilesEnabled(true);
  return { ipc, call, localFiles };
}

function readPort() {
  return {
    describe: () => ({ ok: true }), tree: () => ({ ok: true }), search: () => ({ ok: true }),
    resolve: () => ({ ok: true }), read: () => ({ ok: true }), write: () => ({ ok: true }),
  };
}

test('the preview is published with local files, and not with them closed', async () => {
  // The port *has* the method, so the gate is the only thing that can refuse:
  // a bridge that answered from the port with local files closed would be a
  // bridge that never consulted the switch at all.
  const opened = () => makeBridge({
    ...readPort(),
    previewFile: () => ({ ok: true, name: 'a.html', kind: 'html', size: 1, url: 'cow-preview://preview/t/a.html' }),
  });
  const bridge = await opened();
  bridge.localFiles.setRemoteLocalFilesEnabled(false);
  const off = await bridge.call('getCapabilities');
  assert.equal(off.data.methods.includes('projectPreviewFile'), false,
    'a closed deployment offers no preview');
  assert.equal(off.data.allMethods.includes('projectPreviewFile'), true,
    'the method is part of this bridge version');

  // "not published" and "not accepted" are two different claims, and the second
  // is the one that decides whether a page can reach a local file through a
  // method the panel was never offered.
  const refused = await bridge.call('projectPreviewFile', { workspace_id: 'ws_1', path: 'a.html' });
  assert.equal(refused.ok, false, 'a closed deployment answers nothing but a refusal');
  assert.equal(refused.code, 'feature_unavailable');

  const reopened = await opened();
  const on = await reopened.call('getCapabilities');
  assert.equal(on.data.methods.includes('projectPreviewFile'), true);
  const answered = await reopened.call('projectPreviewFile', { workspace_id: 'ws_1', path: 'a.html' });
  assert.equal(answered.ok, true, 'and with them open the very same call goes through');
});

test('a preview reaches the port as one call, and answers with a URL and no path', async () => {
  const seen = [];
  const bridge = await makeBridge({
    ...readPort(),
    previewFile: (params) => {
      seen.push(params);
      return {
        ok: true,
        name: 'report.html',
        kind: 'html',
        size: 42,
        url: 'cow-preview://preview/token/report.html',
        expires_at: 1700000000000,
      };
    },
  });

  const reply = await bridge.call('projectPreviewFile', { workspace_id: 'ws_1', path: 'out/report.html' });
  assert.equal(reply.ok, true);
  assert.equal(reply.data.url, 'cow-preview://preview/token/report.html');
  assert.equal(reply.data.expires_at, 1700000000000);
  assert.equal(reply.data.name, 'report.html');
  assert.equal(JSON.stringify(reply).includes('ws_1'), false, 'the page is not told what it asked');
  assert.deepEqual(seen, [{ workspace_id: 'ws_1', path: 'out/report.html' }]);
});

test('a malformed preview never reaches the port, and a refusal keeps its code', async () => {
  const seen = [];
  const bridge = await makeBridge({
    ...readPort(),
    previewFile: (params) => { seen.push(params); return { ok: true }; },
  });

  for (const bad of ['/etc/passwd', '../secret', 'C:\\x', 'a/../../b', '']) {
    const reply = await bridge.call('projectPreviewFile', { workspace_id: 'ws_1', path: bad });
    assert.equal(reply.ok, false, bad);
    assert.equal(reply.code, 'invalid_path', bad);
  }
  assert.equal((await bridge.call('projectPreviewFile', { path: 'a.html' })).code, 'invalid_request');
  assert.deepEqual(seen, [], 'nothing malformed is handed to the host');

  const refusing = await makeBridge({
    ...readPort(),
    previewFile: () => ({ ok: false, code: 'unsupported_type', message: 'no isolated preview for this kind' }),
  });
  const refused = await refusing.call('projectPreviewFile', { workspace_id: 'ws_1', path: 'a.pdf' });
  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'unsupported_type');
  assert.equal(refused.message, 'no isolated preview for this kind');
});

test('a host that cannot preview says so by name, never with a silent success', async () => {
  // A host that reads a project but was built before this task: the panel must
  // be able to tell "this build cannot" from "this file cannot".
  const older = await makeBridge(readPort());
  const unavailable = await older.call('projectPreviewFile', { workspace_id: 'ws_1', path: 'a.html' });
  assert.equal(unavailable.ok, false);
  assert.equal(unavailable.code, 'feature_unavailable');

  const noPort = await makeBridge(null);
  assert.equal((await noPort.call('projectPreviewFile', { workspace_id: 'ws_1', path: 'a.html' })).code,
    'feature_unavailable');

  // A host that *has* the method and throws is a broken host, not an absent one:
  // "the device dropped the call" and "this build cannot do it" are different
  // answers, and flattening them tells the user to upgrade when they should retry.
  const throwing = await makeBridge({
    ...readPort(),
    previewFile: () => { throw new Error('the device dropped mid-preview'); },
  });
  const broke = await throwing.call('projectPreviewFile', { workspace_id: 'ws_1', path: 'a.html' });
  assert.equal(broke.ok, false);
  assert.equal(broke.code, 'device_error');
  assert.match(broke.message, /device dropped mid-preview/,
    'the host\'s own reason travels, so the panel is not left guessing');
});

test('the shipping preload exposes the preview, and the bridge publishes the method', () => {
  assert.match(preloadTs, /projectPreviewFile:\s*\(params\?[^)]*\)\s*=>\s*call\('projectPreviewFile'/,
    'the page can only call what the preload exposes');
  assert.match(bridgeTs, /'projectPreviewFile',\n\] as const/,
    'the method is part of PHASE3_METHODS');
  assert.match(bridgeTs, /case 'projectPreviewFile': \{[\s\S]*?checkProjectPath\(params\.path, false\)/,
    'the preview path is shape-checked before it reaches any file API');
});

// ---------------------------------------------------------------------------
// The container: where the scheme is installed, and when it is torn down
// ---------------------------------------------------------------------------

test('the scheme is registered before ready and installed on the container session', () => {
  // Privileges are declared at import (before `app.ready`), which is the only
  // moment Chromium accepts them.
  assert.match(containerTs,
    /protocol\.registerSchemesAsPrivileged\(\[\n\s*\{ scheme: PREVIEW_SCHEME, privileges: PREVIEW_SCHEME_PRIVILEGES \},\n\]\)/);
  // The handler answers on the *container's* session, because that is the
  // document that embeds a preview URL.
  assert.match(containerTs,
    /function installPreviewProtocol\(partition: string\): void \{[\s\S]*?electronSession\.fromPartition\(partition\)[\s\S]*?target\.protocol\.handle\(PREVIEW_SCHEME/);
  assert.match(containerTs, /installPreviewProtocol\(child\.partition\)/,
    'the attach path installs it for the partition the page runs in');
  assert.match(containerTs, /target\.protocol\.unhandle\(PREVIEW_SCHEME\)/,
    'and it is removed with the partition it was installed on');
  // Tickets are per attach: a URL from a previous session must not survive into
  // the next one, and detaching revokes everything outstanding.
  assert.match(containerTs, /previewTickets = createPreviewTickets\(\)/);
  assert.match(containerTs, /previewTickets\.revokeAll\(\)/);
});

test('the Electron adapter maps the pure answer onto a response, and adds nothing', () => {
  assert.match(containerTs,
    /const answer = answerPreviewRequest\(request\.url, previewTickets\)\s*\n\s*return new Response\(answer\.body, \{ status: answer\.status, headers: answer\.headers \}\)/,
    'the one Electron-shaped line: no fallback file, no second policy');
});

test('the scheme privileges are exactly the three the probe measured', () => {
  // These are *values*, and the probe that measured what a page can do with a
  // preview URL registered this same object. `supportFetchAPI` or `corsEnabled`
  // would let the console document `fetch()` a preview URL and read a local
  // file's bytes into script -- the one thing this task exists to prevent -- and
  // `bypassCSP` would make the answer's policy advisory. Pinning the object here
  // is what keeps a future "the probe says images are blocked" from being fixed
  // by granting the privilege instead of by fixing the code.
  assert.deepEqual(preview.PREVIEW_SCHEME_PRIVILEGES, {
    standard: true,
    secure: true,
    stream: true,
  });
  assert.equal(preview.PREVIEW_SCHEME, 'cow-preview');
  // A fixed host, because an opaque host is compared byte-for-byte by Node and
  // case-folded by Chromium: a token in the host would resolve two ways.
  assert.equal(preview.PREVIEW_HOST, 'preview');
  assert.equal(preview.previewUrlFor('t', 'n.html'), 'cow-preview://preview/t/n.html');
});

// ---------------------------------------------------------------------------
// The page: it embeds the URL and never sees the bytes
// ---------------------------------------------------------------------------

test('a local preview asks the host and embeds the answer, and never a server URL', () => {
  // The panel must go through the adapter: `CowProjectSource.preview` is the
  // only thing that knows the live workspace id, and the host is the only thing
  // that knows the bytes.
  assert.match(workspaceJs, /async function wsLocalPreviewTicket\(meta\) \{[\s\S]*?CowProjectSource\.preview\(/);
  assert.match(workspaceJs, /const ticket = await wsLocalPreviewTicket\(meta\);/);
  assert.match(workspaceJs, /frame\.src = ticket\.url;/);

  // For the kinds that need bytes, the element is pointed at the protected URL --
  // not at `rawUrl`, which for a local file does not exist.
  assert.match(workspaceJs, /function wsRenderLocalMedia\(body, tag, ticket, name, epoch, meta\)/);
  assert.match(workspaceJs, /media\.src = ticket\.url;/);
  assert.equal(/local[\s\S]{0,200}?\.src = rawUrl/.test(workspaceJs), false,
    'a local file must never be pointed at a server URL');

  // The URL expires by design, so an image is re-minted once rather than left
  // broken; the second failure is reported with the host's own reason.
  assert.match(workspaceJs, /let retried = false;/);
  assert.match(workspaceJs, /const again = await wsLocalPreviewTicket\(meta\);/);
});

test('a refusal the page can act on is shown with the action that works', () => {
  // Each refusal is a different situation: gone, revoked, offline, too large,
  // and a kind this surface cannot render are five different sentences.
  assert.match(workspaceJs, /if \(code === 'limit_exceeded'\) return t\('ws_local_preview_too_large'\);/);
  assert.match(workspaceJs, /if \(code === 'unsupported_type'\) return t\('ws_local_preview_unsupported'\);/);
  assert.match(workspaceJs, /if \(code === 'not_found'\) return t\('ws_local_file_gone'\);/);
  // The fallback card offers the system application, which is what actually
  // works for a file this shell cannot render.
  assert.match(workspaceJs, /function wsSetLocalPreviewFallback\(body, name, message\)[\s\S]*?onclick="openPreviewExternally\(\)"/);

  // `feature_unavailable`/`no_host` is the *only* fallback to the older
  // behaviour: a real refusal must never be papered over with a stale render.
  // Anchored to the whole body: a lazy `[\s\S]*?` would happily reach the same
  // comparison inside `wsLocalPreviewMessage` below and pass for a function that
  // had stopped making the distinction at all.
  assert.match(workspaceJs,
    /function wsLocalPreviewUnsupported\(refusal\) \{\n\s*const code = \(refusal && refusal\.code\) \|\| '';\n\s*return code === 'feature_unavailable' \|\| code === 'no_host';\n\}/,
    'only a host that cannot preview at all falls back to the older behaviour');
  assert.match(workspaceJs, /else if \(ticket && !wsLocalPreviewUnsupported\(ticket\)\)/);

  // Both the adapter and the host gate the capability rather than assuming it.
  assert.match(projectSourceJs, /function canPreview\(\) \{/);
  assert.match(projectSourceJs, /host\(\)\.canUseProjectPreview\(\)\.catch/);
  assert.match(hostAdapterJs, /canUseProjectPreview: function \(\) \{/);
  assert.match(hostAdapterJs, /var PROJECT_PREVIEW_METHODS = \[\n\s*'projectPreviewFile'\n\s*\];/);

  // Every sentence the panel can print exists in all three languages.
  for (const key of ['ws_local_preview_unsupported', 'ws_local_preview_too_large', 'ws_local_preview_failed']) {
    const uses = coreI18nJs.split(`"${key}":`).length - 1;
    assert.equal(uses, 3, `${key} must be translated in every dictionary`);
  }
});

test('the shell never frames a local file without a sandbox, and never with same-origin', () => {
  // The response carries the policy, but the frame is set here too: two
  // independent decisions about the same document, and neither may drift into
  // `allow-same-origin` -- that one flag is what keeps the console's storage and
  // bridge out of reach of generated HTML.
  assert.match(workspaceJs,
    /frame\.setAttribute\('sandbox', 'allow-scripts allow-forms allow-modals'\)/);
  assert.equal(/setAttribute\('sandbox',[^)]*allow-same-origin/.test(workspaceJs), false,
    'no preview frame may be allowed to be same-origin');
  // The server's own preview keeps the flags it always had.
  assert.match(workspaceJs,
    /frame\.setAttribute\('sandbox', 'allow-scripts allow-popups allow-forms allow-modals'\)/);
});
