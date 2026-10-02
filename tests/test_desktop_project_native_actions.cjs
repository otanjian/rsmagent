// Native actions on a project file (change
// align-desktop-project-execution-with-master, task 9.4, acceptance A25).
//
// "Open this file with my own application" is the one thing the panel cannot do
// for itself, and the one thing that must never be done on a guess: the OS will
// follow whatever path it is handed, through any link, out of the project. So
// the four actions are tested at three levels --
//
//   * the *decision* (`planNativeAction` / `performNativeAction`, the compiled
//     main-process module) against a real directory on a real disk, with fake
//     effects: a file that is gone, a link that points out of the project, a
//     version that moved, a machine with no application for the file;
//   * the *bridge*, driven through the real registered IPC handler with a stub
//     `electron`: what a page may name, and what a refusal is allowed to say;
//   * the *page*, whose buttons have to mean the right thing for a local file
//     and keep meaning the old thing for a server file.
//
// Run: node --test tests/test_desktop_project_native_actions.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const Module = require('node:module');

const ROOT = path.join(__dirname, '..');
const DESKTOP = path.join(ROOT, 'desktop');
const DIST = path.join(DESKTOP, 'dist', 'main');
const SRC = path.join(DESKTOP, 'src', 'main');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');
const workspaceJs = read('channel/web/static/js/workspace.js');
const projectSourceJs = read('channel/web/static/js/fork/project-source.js');
const hostAdapterJs = read('channel/web/static/js/fork/desktop-host.js');

const compiled = {
  native: path.join(DIST, 'project-browser', 'native-actions.js'),
  hostIpc: path.join(DIST, 'remote', 'remote-host-ipc.js'),
  localFiles: path.join(DIST, 'remote', 'local-files-bridge.js'),
};

function needsBuild() {
  const newest = Math.max(
    ...['project-browser/native-actions.ts', 'remote/host-bridge.ts',
      'remote/remote-host-ipc.ts', 'remote/local-files-bridge.ts']
      .map((rel) => fs.statSync(path.join(SRC, rel)).mtimeMs),
  );
  return Object.values(compiled).some(
    (out) => !fs.existsSync(out) || fs.statSync(out).mtimeMs < newest,
  );
}

let native;
before(() => {
  if (needsBuild()) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: DESKTOP, stdio: 'pipe' });
  }
  native = require(compiled.native);
  assert.equal(typeof native.planNativeAction, 'function', 'the planner is compiled');
  assert.equal(typeof native.performNativeAction, 'function', 'the performer is compiled');
});

/**
 * The source of one function, `async` included.
 *
 * `indexOf('function name(')` also matches inside `async function name(`, and
 * dropping the `async` would hand a test a plain value where the shipped code
 * returns a promise.
 */
function extractFunction(src, name) {
  const asyncAt = src.indexOf(`async function ${name}(`);
  const from = asyncAt >= 0 ? asyncAt : src.indexOf(`function ${name}(`);
  assert.ok(from >= 0, `Missing ${name}`);
  let depth = 0;
  for (let i = src.indexOf('{', from); i < src.length; i++) {
    if (src[i] === '{') depth += 1;
    else if (src[i] === '}') {
      depth -= 1;
      if (depth === 0) return src.slice(from, i + 1);
    }
  }
  throw new Error(`Unbalanced ${name}`);
}

// ---------------------------------------------------------------------------
// The decision: a real project on a real disk
// ---------------------------------------------------------------------------

/** A real project directory, already disambiguated from /tmp's own links. */
function makeProject() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-native-')));
  fs.mkdirSync(path.join(dir, 'out'));
  fs.writeFileSync(path.join(dir, 'report.xlsx'), 'PK\u0003\u0004sheet', 'utf8');
  fs.writeFileSync(path.join(dir, 'out', 'summary.csv'), 'a,b\n1,2\n', 'utf8');
  return dir;
}

/** Effects that record what they were asked to do, and never touch the system. */
function fakeEffects(overrides = {}) {
  const seen = { opened: [], revealed: [], copied: [], copiedFiles: [], dialogs: [] };
  const effects = {
    openPath: async (p) => { seen.opened.push(p); return ''; },
    revealPath: (p) => { seen.revealed.push(p); },
    copyPath: (p) => { seen.copied.push(p); },
    chooseDestination: async (name) => {
      seen.dialogs.push(name);
      return path.join(os.tmpdir(), `copy-of-${name}`);
    },
    copyFile: async (from, to) => { seen.copiedFiles.push([from, to]); },
    ...overrides,
  };
  return { effects, seen };
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

/** Plan one action against a real project, the way the container port does. */
function plan(root, rel, kind, options = {}) {
  const { entry = {}, ...rest } = options;
  return native.planNativeAction({
    kind,
    path: rel,
    root,
    entry: entry.ok === false ? entry : entryFor(root, rel, entry),
    ...rest,
  });
}

test('open hands the system the verified absolute path and answers with a name', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const { effects, seen } = fakeEffects();

  const answer = await native.performNativeAction(plan(dir, 'out/summary.csv', 'open'), effects);
  assert.equal(answer.ok, true);
  assert.equal(answer.opened, true);
  assert.equal(answer.name, 'summary.csv');
  assert.deepEqual(seen.opened, [path.join(dir, 'out', 'summary.csv')]);
  // The whole point: the directory is *used*, never returned.
  assert.equal(JSON.stringify(answer).includes(dir), false,
    'the reply must not carry the project directory');
});

test('an operating system failure is reported as the system described it', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  const noApp = fakeEffects({
    openPath: async () => 'The application cannot be opened for an unexpected reason',
  });
  const refused = await native.performNativeAction(
    plan(dir, 'report.xlsx', 'open'), noApp.effects,
  );
  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'no_application',
    'a machine with no application for this kind of file is its own answer');
  assert.match(refused.message, /application cannot be opened/,
    'the system\'s own words travel with it, not a flattened code');

  const denied = fakeEffects({ openPath: async () => 'EACCES: permission denied' });
  assert.equal((await native.performNativeAction(plan(dir, 'report.xlsx', 'open'), denied.effects)).code,
    'permission_denied');
  const thrown = fakeEffects({ openPath: async () => { throw new Error('spawn failed'); } });
  const thrownAnswer = await native.performNativeAction(plan(dir, 'report.xlsx', 'open'), thrown.effects);
  assert.equal(thrownAnswer.code, 'device_error');
  assert.match(thrownAnswer.message, /spawn failed/);
});

test('reveal and copy-path act on the machine and keep the path out of the reply', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const { effects, seen } = fakeEffects();

  const revealed = await native.performNativeAction(plan(dir, 'out', 'reveal'), effects);
  assert.equal(revealed.ok, true);
  assert.equal(revealed.opened, true);
  assert.equal(revealed.is_directory, true);
  assert.deepEqual(seen.revealed, [path.join(dir, 'out')]);

  const copied = await native.performNativeAction(plan(dir, 'report.xlsx', 'copyPath'), effects);
  assert.equal(copied.copied, true);
  // Copied *here*, by this process: the page asked for the action, not for the
  // string, and it is never told where the file is.
  assert.deepEqual(seen.copied, [path.join(dir, 'report.xlsx')]);
  assert.equal(JSON.stringify(copied).includes(dir), false);
});

test('save-as writes a real copy elsewhere and names only the copy', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const destination = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-copy-')), 'renamed.csv');
  t.after(() => fs.rmSync(path.dirname(destination), { recursive: true, force: true }));
  const { effects, seen } = fakeEffects({
    chooseDestination: async (name) => { seen.dialogs.push(name); return destination; },
    // The real copy, so the test proves a file actually appears where the user
    // asked for it -- a recorder here would only prove the call was made.
    copyFile: (from, to) => fs.promises.copyFile(from, to),
  });

  const answer = await native.performNativeAction(plan(dir, 'out/summary.csv', 'saveAs'), effects);
  assert.equal(answer.ok, true);
  assert.equal(answer.saved, true);
  assert.equal(answer.name, 'renamed.csv');
  assert.deepEqual(seen.dialogs, ['summary.csv'], 'the dialog is offered the file\'s own name');
  assert.equal(fs.readFileSync(destination, 'utf8'), fs.readFileSync(path.join(dir, 'out', 'summary.csv'), 'utf8'));
  // The chosen *directory* is where the user just put the file; it is not this
  // process's to publish either.
  assert.equal(JSON.stringify(answer).includes(path.dirname(destination)), false);
});

test('a copy is refused while the file has moved on, unless the user says so', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const { effects, seen } = fakeEffects();
  const planned = plan(dir, 'report.xlsx', 'open');
  const copyPlan = plan(dir, 'report.xlsx', 'saveAs');
  const stale = copyPlan.modified - 5;

  const refused = await native.performNativeAction(copyPlan, effects, { expectedMtime: stale });
  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'changed');
  assert.match(refused.message, new RegExp(String(stale)),
    'the two versions are named, so the user can see what changed');
  assert.deepEqual(seen.dialogs, [], 'nothing is even asked for before the check passes');

  const accepted = await native.performNativeAction(copyPlan, effects, {
    expectedMtime: stale, acceptCurrent: true,
  });
  assert.equal(accepted.ok, true, 'the user\'s answer is what settles it');
  assert.equal(accepted.saved, true);

  const matching = await native.performNativeAction(plan(dir, 'report.xlsx', 'saveAs'), effects, {
    expectedMtime: planned.modified,
  });
  assert.equal(matching.ok, true, 'the same version is not a conflict');
});

test('a dismissed save dialog is an answer, not a failure to explain', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const { effects, seen } = fakeEffects({ chooseDestination: async () => null });

  const answer = await native.performNativeAction(plan(dir, 'report.xlsx', 'saveAs'), effects);
  assert.equal(answer.ok, false);
  assert.equal(answer.code, 'cancelled');
  assert.deepEqual(seen.copiedFiles, [], 'nothing is written when the user says no');
});

test('a copy onto the file itself is refused rather than truncated', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const { effects } = fakeEffects({
    chooseDestination: async () => path.join(dir, 'report.xlsx'),
  });

  const answer = await native.performNativeAction(plan(dir, 'report.xlsx', 'saveAs'), effects);
  assert.equal(answer.ok, false);
  assert.equal(answer.code, 'invalid_request');
  assert.equal(fs.readFileSync(path.join(dir, 'report.xlsx'), 'utf8'), 'PK\u0003\u0004sheet',
    'the original is untouched');
});

test('a file that is gone is refused, not opened', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const entry = entryFor(dir, 'report.xlsx');
  fs.rmSync(path.join(dir, 'report.xlsx'));
  const { effects, seen } = fakeEffects();

  const refused = native.planNativeAction({ kind: 'open', path: 'report.xlsx', root: dir, entry });
  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'not_found');
  // Handing a refusal to the performer gives the refusal back, so no caller can
  // crash (or act) by forgetting the check.
  const answer = await native.performNativeAction(refused, effects);
  assert.equal(answer.code, 'not_found');
  assert.deepEqual(seen.opened, [], 'the system is never asked to open a path that is gone');
});

test('an entry that is a link, or a directory asked to be saved as a file, is refused', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.symlinkSync('/etc/hosts', path.join(dir, 'hosts-link'));

  const link = plan(dir, 'hosts-link', 'open');
  assert.equal(link.ok, false);
  assert.equal(link.code, 'not_a_file');

  const asFile = plan(dir, 'out', 'saveAs');
  assert.equal(asFile.ok, false);
  assert.equal(asFile.code, 'not_a_file');
});

test('an entry that resolves outside the project is refused even if the helper allowed it', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const outside = makeProject();
  t.after(() => fs.rmSync(outside, { recursive: true, force: true }));
  // A directory *inside* the project that is really a link out of it. A check
  // that only joined the strings would happily open the file behind it.
  fs.symlinkSync(outside, path.join(dir, 'escape'));

  const answer = plan(dir, 'escape/report.xlsx', 'open');
  assert.equal(answer.ok, false);
  assert.equal(answer.code, 'unsafe_path');

  // The same path when the link is named directly: the helper classifies a link
  // as a link, and that refusal is the one that travels.
  const viaLink = plan(dir, 'escape', 'open');
  assert.equal(viaLink.ok, false);
  assert.equal(viaLink.code, 'not_a_file');
});

test('an entry whose kind changed on disk is refused rather than acted on', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  // The helper said "file" (that is what the panel read); the disk now says
  // "directory". Opening it would open something the user has not seen.
  const answer = native.planNativeAction({
    kind: 'open', path: 'out', root: dir, entry: entryFor(dir, 'out', { kind: 'file' }),
  });
  assert.equal(answer.ok, false);
  assert.equal(answer.code, 'changed');
});

test('the helper\'s own refusal is what the user is told', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  const revoked = plan(dir, 'report.xlsx', 'open', {
    entry: { ok: false, code: 'stale_context', message: 'no local project is open in this session' },
  });
  assert.equal(revoked.ok, false);
  assert.equal(revoked.code, 'stale_context');
  assert.equal(revoked.message, 'no local project is open in this session');

  const missing = plan(dir, 'report.xlsx', 'open', {
    entry: { ok: false, code: 'not_found', message: 'this file is no longer on disk' },
  });
  assert.equal(missing.code, 'not_found', 'a file the helper cannot see is not opened either');
});

test('no grant root, or a root that is gone, is a revoked project', async (t) => {
  const dir = makeProject();
  const { effects } = fakeEffects();
  const entry = entryFor(dir, 'report.xlsx');

  const noRoot = native.planNativeAction({ kind: 'open', path: 'report.xlsx', root: null, entry });
  assert.equal(noRoot.ok, false);
  assert.equal(noRoot.code, 'stale_context');
  const noRootAnswer = await native.performNativeAction(noRoot, effects);
  assert.equal(noRootAnswer.code, 'stale_context');
  assert.match(noRootAnswer.message, /no longer authorized/);

  fs.rmSync(dir, { recursive: true, force: true });
  const goneRoot = native.planNativeAction({ kind: 'open', path: 'report.xlsx', root: dir, entry });
  assert.equal(goneRoot.code, 'stale_context');
  assert.match(goneRoot.message, /no longer reachable/);
});

test('a path that could never be project-relative is refused before any check', async (t) => {
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const permissive = () => ({ ok: true, kind: 'file', size: 1, modified: 1 });

  for (const bad of ['/etc/passwd', '../secret', 'C:\\Windows\\x', 'a/../../b', '', 'a\u0000b']) {
    const answer = native.planNativeAction({ kind: 'open', path: bad, root: dir, entry: permissive() });
    assert.equal(answer.ok, false, JSON.stringify(bad));
    assert.equal(answer.code, 'invalid_request', JSON.stringify(bad));
  }
  const unknown = native.planNativeAction({ kind: 'launch', path: 'report.xlsx', root: dir, entry: permissive() });
  assert.equal(unknown.code, 'invalid_request', 'the action vocabulary is closed');
});

// ---------------------------------------------------------------------------
// The bridge: what a page may ask for, and what it may be told
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

test('the four actions are published with local files, and not without them', async () => {
  const bridge = await makeBridge(null);
  bridge.localFiles.setRemoteLocalFilesEnabled(false);
  const off = await bridge.call('getCapabilities');
  for (const method of ['projectOpenFile', 'projectRevealFile', 'projectCopyPath', 'projectSaveFileAs']) {
    assert.equal(off.data.methods.includes(method), false, `${method} is not offered by a closed deployment`);
    assert.equal(off.data.allMethods.includes(method), true, `${method} is part of this bridge version`);
  }
  bridge.localFiles.setRemoteLocalFilesEnabled(true);
  const on = await bridge.call('getCapabilities');
  for (const method of ['projectOpenFile', 'projectRevealFile', 'projectCopyPath', 'projectSaveFileAs']) {
    assert.equal(on.data.methods.includes(method), true, method);
  }
});

test('each method reaches the port as its own action, with the page\'s parameters', async () => {
  const seen = [];
  const bridge = await makeBridge({
    describe: () => ({ ok: true }), tree: () => ({ ok: true }), search: () => ({ ok: true }),
    resolve: () => ({ ok: true }), read: () => ({ ok: true }), write: () => ({ ok: true }),
    nativeAction: (action, params) => {
      seen.push({ action, params });
      return { ok: true, name: 'report.xlsx' };
    },
  });
  for (const [method, action] of [
    ['projectOpenFile', 'open'], ['projectRevealFile', 'reveal'],
    ['projectCopyPath', 'copyPath'], ['projectSaveFileAs', 'saveAs'],
  ]) {
    const reply = await bridge.call(method, { workspace_id: 'ws_1', path: 'out/summary.csv' });
    assert.equal(reply.ok, true, method);
    assert.equal(reply.data.name, 'report.xlsx');
    assert.equal(seen.at(-1).action, action, method);
    assert.equal(seen.at(-1).params.path, 'out/summary.csv');
  }
  assert.equal(seen.length, 4);
});

test('a malformed action never reaches the port', async () => {
  const seen = [];
  const bridge = await makeBridge({
    describe: () => ({ ok: true }), tree: () => ({ ok: true }), search: () => ({ ok: true }),
    resolve: () => ({ ok: true }), read: () => ({ ok: true }), write: () => ({ ok: true }),
    nativeAction: (action, params) => { seen.push([action, params]); return { ok: true }; },
  });
  for (const bad of ['/etc/passwd', '../secret', 'C:\\x', 'a/../../b', '']) {
    const reply = await bridge.call('projectOpenFile', { workspace_id: 'ws_1', path: bad });
    assert.equal(reply.ok, false, bad);
    assert.equal(reply.code, 'invalid_path', bad);
  }
  const noWorkspace = await bridge.call('projectOpenFile', { path: 'a.txt' });
  assert.equal(noWorkspace.code, 'invalid_request');
  const badVersion = await bridge.call('projectSaveFileAs', {
    workspace_id: 'ws_1', path: 'a.txt', expected_mtime: -1,
  });
  assert.equal(badVersion.code, 'invalid_request');
  const badFlag = await bridge.call('projectSaveFileAs', {
    workspace_id: 'ws_1', path: 'a.txt', accept_current: 'yes',
  });
  assert.equal(badFlag.code, 'invalid_request');
  assert.deepEqual(seen, [], 'nothing malformed is handed to the host');
});

test('a refusal keeps its own code, and a host without the action refuses by name', async () => {
  const bridge = await makeBridge({
    describe: () => ({ ok: true }), tree: () => ({ ok: true }), search: () => ({ ok: true }),
    resolve: () => ({ ok: true }), read: () => ({ ok: true }), write: () => ({ ok: true }),
    nativeAction: () => ({ ok: false, code: 'no_application', message: 'the system has no application for this file' }),
  });
  const refused = await bridge.call('projectOpenFile', { workspace_id: 'ws_1', path: 'a.xlsx' });
  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'no_application');
  assert.equal(refused.message, 'the system has no application for this file');

  // A port that reads a project but cannot act on it is the older build: the
  // answer is "this host cannot", never a silent success.
  const readOnly = await makeBridge({
    describe: () => ({ ok: true }), tree: () => ({ ok: true }), search: () => ({ ok: true }),
    resolve: () => ({ ok: true }), read: () => ({ ok: true }), write: () => ({ ok: true }),
  });
  const unavailable = await readOnly.call('projectRevealFile', { workspace_id: 'ws_1', path: 'a.txt' });
  assert.equal(unavailable.ok, false);
  assert.equal(unavailable.code, 'feature_unavailable');

  const noPort = await makeBridge(null);
  const nothing = await noPort.call('projectCopyPath', { workspace_id: 'ws_1', path: 'a.txt' });
  assert.equal(nothing.code, 'feature_unavailable');
});

// ---------------------------------------------------------------------------
// The adapter: what the panel asks
// ---------------------------------------------------------------------------

/** `fork/project-source.js` in a fake page, as the panel loads it. */
function loadAdapter(options = {}) {
  const calls = [];
  const window = {
    URL,
    CowDesktopHost: options.host === null ? undefined : {
      project: () => Promise.resolve({ ok: true }),
      canUseProjectSource: () => Promise.resolve(true),
      ...(options.host || {}),
      projectAction: options.acts
        ? (action, params) => {
          calls.push({ action, params });
          const answer = options.acts(action, params);
          return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
        }
        : undefined,
      canUseProjectActions: options.canAct === undefined
        ? () => Promise.resolve(true)
        : () => Promise.resolve(options.canAct),
    },
  };
  const api = new Function('window', 'module', `${projectSourceJs}\nreturn window.CowProjectSource;`)(
    window, { exports: {} },
  );
  api.configure({
    binding: () => (options.binding === undefined ? { workspace_id: 'ws_1' } : options.binding),
    kindOf: () => 'text',
    editable: () => true,
    previewable: () => true,
  });
  return { api, calls };
}

test('an action carries the live workspace, and a refusal comes back as an answer', async () => {
  const { api, calls } = loadAdapter({
    acts: () => ({ ok: true, copied: true, name: 'summary.csv' }),
  });
  const reply = await api.act('copyPath', 'out/summary.csv');
  assert.deepEqual(calls[0], {
    action: 'copyPath',
    params: { workspace_id: 'ws_1', path: 'out/summary.csv' },
  });
  assert.equal(reply.ok, true);
  assert.equal(reply.copied, true);

  const asking = loadAdapter({
    acts: () => ({ ok: false, code: 'not_found', message: 'this file is no longer on disk' }),
  });
  const refused = await asking.api.act('open', 'gone.txt');
  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'not_found');
  assert.equal(refused.message, 'this file is no longer on disk');

  // The version the panel read travels with a save: it is how the host knows
  // whether the user has actually seen what would be copied.
  const saving = loadAdapter({ acts: () => ({ ok: true, saved: true }) });
  await saving.api.act('saveAs', 'a.csv', { expectedMtime: 42, acceptCurrent: true });
  assert.equal(saving.calls[0].params.expected_mtime, 42);
  assert.equal(saving.calls[0].params.accept_current, true);
});

test('an action without a project, or without a path, is refused by name', async () => {
  const noBinding = loadAdapter({ binding: null, acts: () => ({ ok: true }) });
  const reply = await noBinding.api.act('open', 'a.txt');
  assert.equal(reply.ok, false);
  assert.equal(reply.code, 'no_host');
  assert.deepEqual(noBinding.calls, []);

  const noPath = loadAdapter({ acts: () => ({ ok: true }) });
  assert.equal((await noPath.api.act('open', '')).code, 'invalid_request');
  assert.deepEqual(noPath.calls, []);

  // A thrown refusal from the bridge (rather than a resolved one) is turned
  // into the same answer, so the panel has one shape to read.
  const thrown = loadAdapter({
    acts: () => Object.assign(new Error('the host refused the call'), { code: 'stale_context' }),
  });
  const failed = await thrown.api.act('reveal', 'a.txt');
  assert.equal(failed.code, 'stale_context');
  assert.equal(failed.message, 'the host refused the call');

  const older = loadAdapter({ host: { projectAction: undefined } });  assert.equal((await older.api.act('open', 'a.txt')).code, 'no_host');
  assert.equal(await older.api.canAct(), false);
});

// ---------------------------------------------------------------------------
// The page: buttons that tell the truth
// ---------------------------------------------------------------------------

/** The panel's action helpers, in a sandbox, with the shipped code extracted. */
function loadPanel(overrides = {}) {
  const toasts = [];
  const ctx = {
    console: { log() {}, warn() {}, error() {} },
    Promise, JSON, String, Number, Object, Array, isFinite, setTimeout, clearTimeout,
    _wsToast: (message) => toasts.push(String(message)),
    t: (key) => key,
    wsCurrentFile: null,
    wsEditTargetPath: (meta) => meta.abs_path || meta.path || meta.rel_path || '',
    window: { confirm: () => (overrides.confirm === undefined ? true : overrides.confirm) },
    CowProjectSource: {
      act: async (action, p, options) => {
        (overrides.acts || []).push({ action, path: p, options });
        return overrides.answer ? overrides.answer(action, p, options) : { ok: true };
      },
      preview: async (p, options) => {
        (overrides.previews || []).push({ path: p, options });
        return overrides.previewAnswer
          ? overrides.previewAnswer(p, options)
          : { ok: true, url: 'cow-preview://x' };
      },
    },
  };
  vm.createContext(ctx);
  vm.runInContext([
    extractFunction(workspaceJs, 'wsEditTargetPath'),
    extractFunction(workspaceJs, 'wsLocalAction'),
    extractFunction(workspaceJs, 'wsLocalCardIsGone'),
    extractFunction(workspaceJs, 'wsLocalCardScope'),
    extractFunction(workspaceJs, 'wsLocalActionMessage'),
    extractFunction(workspaceJs, 'wsLocalPreviewTicket'),
    extractFunction(workspaceJs, 'wsLocalPreviewMessage'),
    extractFunction(workspaceJs, 'wsLocalConfirm'),
    extractFunction(workspaceJs, 'wsSaveLocalCopyOnce'),
    extractFunction(workspaceJs, 'wsSaveLocalCopy'),
  ].join('\n'), ctx);
  return {
    toasts,
    acts: overrides.acts || [],
    previews: overrides.previews || [],
    action: (meta, action, options) => vm.runInContext(
      `wsLocalAction(${JSON.stringify(meta)}, ${JSON.stringify(action)}, ${JSON.stringify(options || {})})`, ctx,
    ),
    preview: (meta) => vm.runInContext(
      `wsLocalPreviewTicket(${JSON.stringify(meta)})`, ctx,
    ),
    message: (refusal) => vm.runInContext(
      `wsLocalActionMessage(${JSON.stringify(refusal)})`, ctx,
    ),
    previewMessage: (refusal) => vm.runInContext(
      `wsLocalPreviewMessage(${JSON.stringify(refusal)})`, ctx,
    ),
    save: (meta) => vm.runInContext(`wsSaveLocalCopy(${JSON.stringify(meta)})`, ctx),
  };
}

test('the four reasons a system action did not happen are told apart', () => {
  const panel = loadPanel();
  const messages = {
    not_found: panel.message({ code: 'not_found' }),
    stale_context: panel.message({ code: 'stale_context' }),
    device_offline: panel.message({ code: 'device_offline' }),
    no_application: panel.message({ code: 'no_application' }),
    unavailable: panel.message({ code: 'feature_unavailable' }),
    forbidden: panel.message({ code: 'permission_denied' }),
    other: panel.message({ code: 'weird' }),
  };
  for (const [name, text] of Object.entries(messages)) {
    assert.ok(text && text.length > 0, name);
  }
  const unique = new Set(Object.values(messages));
  assert.equal(unique.size, Object.keys(messages).length,
    'each refusal has its own sentence, not one shared "it failed"');
  assert.equal(messages.not_found, 'ws_local_file_gone');
  assert.equal(messages.stale_context, 'ws_local_stale');
  assert.equal(messages.no_application, 'ws_local_no_application');
  assert.equal(messages.unavailable, 'ws_local_action_unavailable');
  // The system's own explanation is kept when there is one.
  assert.match(panel.message({ code: 'no_application', message: 'no such application' }),
    /no such application/);
});

test('saving a copy asks before it copies a version the user has not seen', async () => {
  const meta = { path: 'out/summary.csv', mtime: 100 };

  const changedThenAccepted = [];
  const retry = loadPanel({
    confirm: true,
    acts: changedThenAccepted,
    answer: (action, p, options) => (options.acceptCurrent
      ? { ok: true, saved: true }
      : { ok: false, code: 'changed', message: 'version 100 is now 120' }),
  });
  await retry.save(meta);
  assert.deepEqual(changedThenAccepted.map((c) => c.options.acceptCurrent), [false, true],
    'the second attempt is the one the user agreed to');
  assert.deepEqual(retry.toasts, ['ws_local_saved_as']);

  const declined = [];
  const refused = loadPanel({
    confirm: false,
    acts: declined,
    answer: () => ({ ok: false, code: 'changed', message: 'version 100 is now 120' }),
  });
  await refused.save(meta);
  assert.equal(declined.length, 1, 'a declined question is not asked twice');
  assert.deepEqual(refused.toasts, [], 'declining a copy is not an error to announce');

  const gone = loadPanel({ answer: () => ({ ok: false, code: 'not_found' }) });
  await gone.save(meta);
  assert.deepEqual(gone.toasts, ['ws_local_file_gone']);

  const cancelled = loadPanel({ answer: () => ({ ok: false, code: 'cancelled' }) });
  await cancelled.save(meta);
  assert.deepEqual(cancelled.toasts, [], 'a dismissed save dialog needs no message');

  const ok = loadPanel({ answer: () => ({ ok: true, saved: true }) });
  await ok.save(meta);
  assert.deepEqual(ok.toasts, ['ws_local_saved_as']);
});

/** The preview header, in a sandbox with a stub document. */
function loadHeader(meta) {
  const elements = {};
  const ids = ['ws-btn-external', 'ws-btn-download', 'ws-btn-copy', 'ws-btn-reveal',
    'ws-btn-edit', 'ws-btn-save', 'ws-btn-edit-cancel'];
  for (const id of ids) {
    elements[id] = {
      id,
      hidden: true,
      title: '',
      dataset: { i18nTitle: '' },
      classList: {
        toggle: (klass, force) => { if (klass === 'hidden') elements[id].hidden = !!force; },
      },
    };
  }
  const ctx = {
    console: { log() {}, warn() {}, error() {} },
    String, JSON, Object, Array,
    t: (key) => key,
    wsActiveTab: 'preview',
    wsEditing: false,
    wsCurrentFile: meta,
    wsIsEditable: () => true,
    document: { getElementById: (id) => elements[id] || null },
  };
  vm.createContext(ctx);
  vm.runInContext([
    extractFunction(workspaceJs, 'wsIsLocalFile'),
    extractFunction(workspaceJs, 'wsRetitle'),
    extractFunction(workspaceJs, 'wsUpdateHeaderActions'),
    'wsUpdateHeaderActions();',
  ].join('\n'), ctx);
  return elements;
}

test('a local file\'s buttons say what they will do, and a server file\'s keep saying theirs', () => {
  const local = loadHeader({ path: 'out/summary.csv', local: true, mtime: 1 });
  assert.equal(local['ws-btn-external'].title, 'ws_open_system');
  assert.equal(local['ws-btn-download'].title, 'ws_save_as');
  assert.equal(local['ws-btn-copy'].title, 'ws_local_copy_path');
  assert.equal(local['ws-btn-reveal'].hidden, false, 'revealing a local file is offered');
  // Both attributes, so a later language switch keeps the right label.
  assert.equal(local['ws-btn-external'].dataset.i18nTitle, 'ws_open_system');

  const server = loadHeader({ path: 'reports/a.csv', abs_path: '/srv/reports/a.csv', kind: 'csv' });
  assert.equal(server['ws-btn-external'].title, 'ws_open_external');
  assert.equal(server['ws-btn-download'].title, 'ws_download');
  assert.equal(server['ws-btn-copy'].title, 'ws_copy_path');
  assert.equal(server['ws-btn-reveal'].hidden, true,
    'a server file has no folder on this machine to reveal');

  const none = loadHeader(null);
  for (const id of ['ws-btn-external', 'ws-btn-download', 'ws-btn-copy', 'ws-btn-reveal']) {
    assert.equal(none[id].hidden, true, `${id} with nothing open`);
  }
});

test('a local file card carries the system actions instead of a server download', () => {
  const ctx = {
    console: { log() {}, warn() {}, error() {} },
    String, JSON, Object, Array,
    t: (key) => key,
    escapeHtml: (value) => String(value),
    wsKindOf: () => 'csv',
    wsIconClass: () => 'i',
    wsFormatSize: () => '',
    WS_PREVIEWABLE: new Set(['csv']),
  };
  vm.createContext(ctx);
  vm.runInContext([
    extractFunction(workspaceJs, 'wsIsLocalFile'),
    extractFunction(workspaceJs, 'renderFileCard'),
  ].join('\n'), ctx);
  const card = (meta) => vm.runInContext(`renderFileCard(${JSON.stringify(meta)})`, ctx);

  const local = card({ file_name: 'summary.csv', rel_path: 'out/summary.csv', local: true, mtime: 3 });
  for (const action of ['local-open', 'local-reveal', 'local-copy-path', 'local-save-as']) {
    assert.ok(local.includes(`data-action="${action}"`), action);
  }
  assert.equal(local.includes('data-action="download"'), false,
    'a server download is not offered for a file that has no server URL');

  const server = card({
    file_name: 'a.csv', rel_path: 'reports/a.csv',
    raw_url: '/api/workspace/read?path=reports/a.csv',
  });
  assert.equal(server.includes('data-action="download"'), true);
  assert.equal(server.includes('data-action="local-open"'), false,
    'a server file is not opened by the host');
});

// ---------------------------------------------------------------------------
// History cards: a file that belongs to another project (task 9.6)
// ---------------------------------------------------------------------------

test('an action on a file from another project is refused, not re-resolved', async () => {
  const { api, calls } = loadAdapter({
    binding: { workspace_id: 'ws_now' },
    acts: () => ({ ok: true, opened: true }),
  });

  const reply = await api.act('open', 'out/report.xlsx', { expectedWorkspaceId: 'ws_then' });

  assert.equal(reply.ok, false);
  assert.equal(reply.code, 'wrong_project');
  assert.deepEqual(calls, [],
    'the host must not be asked to resolve the old path against the project open now');
});

test('a preview of a file from another project is refused the same way', async () => {
  const previews = [];
  const { api } = loadAdapter({
    binding: { workspace_id: 'ws_now' },
    host: {
      projectPreview: (params) => { previews.push(params); return { ok: true, url: 'cow-preview://x' }; },
    },
  });

  const refused = await api.preview('out/report.xlsx', { expectedWorkspaceId: 'ws_then' });

  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'wrong_project');
  assert.deepEqual(previews, []);
});

test('the card\'s own project is the one acted on when it is still the open one', async () => {
  const { api, calls } = loadAdapter({
    binding: { workspace_id: 'ws_same' },
    acts: () => ({ ok: true, copied: true }),
  });

  const reply = await api.act('copyPath', 'out/report.xlsx', { expectedWorkspaceId: 'ws_same' });

  assert.equal(reply.ok, true);
  assert.equal(calls[0].params.workspace_id, 'ws_same');
});

test('a live card, which knows no owner, keeps the binding\'s own project', async () => {
  const { api, calls } = loadAdapter({ acts: () => ({ ok: true }) });

  await api.act('reveal', 'out/summary.csv');

  assert.equal(calls[0].params.workspace_id, 'ws_1');
});

test('the panel refuses a card the server already reported as gone', async () => {
  const panel = loadPanel({ acts: [] });
  const outcome = await panel.action({ rel_path: 'out/report.xlsx', local: true, resolution: 'missing' }, 'open');

  assert.equal(outcome.ok, false);
  assert.equal(outcome.code, 'not_found');
  assert.deepEqual(panel.acts, [],
    'a replayed card that is known to be gone must not be re-resolved against an open project');
});

test('the panel hands the card\'s project to the adapter, and a live card nothing', async () => {
  const panel = loadPanel({ acts: [] });

  await panel.action({ rel_path: 'out/report.xlsx', local: true, workspace_id: 'ws_then' }, 'open');
  assert.equal(panel.acts[0].options.expectedWorkspaceId, 'ws_then');
  assert.deepEqual(Object.keys(panel.acts[0].options), ['expectedWorkspaceId']);

  await panel.action({ rel_path: 'out/summary.csv', local: true, resolution: 'ok' }, 'open');
  assert.deepEqual(Object.keys(panel.acts[1].options), [],
    'a live card has no owner to name, so nothing is added');
});

test('"another project" is its own sentence, not a generic failure', () => {
  const panel = loadPanel();
  assert.equal(panel.message({ code: 'wrong_project' }), 'ws_local_other_project');
  assert.notEqual(panel.message({ code: 'wrong_project' }), panel.message({ code: 'stale_context' }));
  assert.notEqual(panel.message({ code: 'wrong_project' }), panel.message({ code: 'not_found' }));
  assert.equal(panel.previewMessage({ code: 'wrong_project' }), 'ws_local_other_project');
});

test('the panel refuses to preview a card the server reported as gone', async () => {
  const panel = loadPanel({ previews: [] });
  const ticket = await panel.preview({ rel_path: 'out/report.xlsx', local: true, resolution: 'missing' });

  assert.equal(ticket.ok, false);
  assert.equal(ticket.code, 'not_found');
  assert.deepEqual(panel.previews, []);
});

test('a preview carries the card\'s project, and a live card\'s does not', async () => {
  const panel = loadPanel({ previews: [] });

  await panel.preview({ rel_path: 'out/report.xlsx', local: true, workspace_id: 'ws_then' });
  assert.equal(panel.previews[0].options.expectedWorkspaceId, 'ws_then');

  await panel.preview({ rel_path: 'out/summary.csv', local: true });
  assert.deepEqual(Object.keys(panel.previews[1].options), []);
});

test('a card carries the project and the resolution it was rebuilt with', () => {
  const ctx = {
    console: { log() {}, warn() {}, error() {} },
    String, JSON, Object, Array,
    t: (key) => key,
    escapeHtml: (value) => String(value),
    wsKindOf: () => 'office',
    wsIconClass: () => 'i',
    wsFormatSize: () => '',
    WS_PREVIEWABLE: new Set(['office']),
  };
  vm.createContext(ctx);
  vm.runInContext([
    extractFunction(workspaceJs, 'wsIsLocalFile'),
    extractFunction(workspaceJs, 'renderFileCard'),
  ].join('\n'), ctx);
  const card = (meta) => vm.runInContext(`renderFileCard(${JSON.stringify(meta)})`, ctx);

  const replayed = card({
    file_name: 'report.xlsx', rel_path: 'out/report.xlsx', local: true,
    workspace_id: 'ws_then', resolution: 'missing',
  });
  const carried = JSON.parse(
    replayed.match(/data-file='([\s\S]*?)'/)[1].replace(/&quot;/g, '"'));

  assert.equal(carried.workspace_id, 'ws_then');
  assert.equal(carried.resolution, 'missing');

  const live = card({ file_name: 'a.xlsx', rel_path: 'a.xlsx', local: true });
  const liveCarried = JSON.parse(
    live.match(/data-file='([\s\S]*?)'/)[1].replace(/&quot;/g, '"'));
  assert.equal(liveCarried.workspace_id, '');
  assert.equal(liveCarried.resolution, 'ok');
});

// ---------------------------------------------------------------------------
// The page: a local project says where its data goes (task 9.7)
// ---------------------------------------------------------------------------

/** The breadcrumb, in a sandbox, with the shipped code extracted. */
function loadBreadcrumb(overrides = {}) {
  const bar = { innerHTML: '' };
  const ctx = {
    console: { log() {}, warn() {}, error() {} },
    String, Object, Array,
    escapeHtml: (value) => String(value === undefined || value === null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;'),
    t: (key) => key,
    wsCurrentRoot: overrides.root || '',
    wsCurrentSource: overrides.source || 'backend',
    wsLocalRootLabel: () => overrides.label || 'ws_sel_local_dir',
    document: { getElementById: (id) => (id === 'ws-breadcrumb' ? bar : null) },
  };
  vm.createContext(ctx);
  vm.runInContext(extractFunction(workspaceJs, 'renderWorkspaceBreadcrumb'), ctx);
  return {
    render: (relPath) => {
      vm.runInContext(`renderWorkspaceBreadcrumb(${JSON.stringify(relPath || '')})`, ctx);
      return bar.innerHTML;
    },
  };
}

test('a local project crumb states the data flow, and a server one does not', () => {
  const local = loadBreadcrumb({ source: 'desktop', root: '', label: 'my-project' }).render('');
  assert.match(local, /data-tooltip="[^"]*my-project/);
  assert.match(local, /ws_local_data_flow/,
    'the crumb explains that outputs and metadata go back to the server');

  // The promise is about *local* files. A server path is already on the server,
  // so repeating it there would be noise, not information.
  const server = loadBreadcrumb({ source: 'backend', root: '/work/user/1' }).render('');
  assert.match(server, /data-tooltip="\/work\/user\/1"/);
  assert.doesNotMatch(server, /ws_local_data_flow/);
});

test('the three dictionaries carry the data-flow statement and agree on its shape', () => {
  const core = read('channel/web/static/js/i18n/core.js');
  const matches = core.match(/"ws_local_data_flow":\s*"((?:[^"\\]|\\.)*)"/g) || [];
  assert.equal(matches.length, 3, 'zh, zh-Hant and en each declare it');
  for (const entry of matches) {
    const text = entry.slice(entry.indexOf(':') + 1).trim();
    assert.ok(text.length > 40, `the statement is a sentence, not a label: ${text}`);
  }
  // The claim that must never be made: "nothing leaves this machine".
  const joined = matches.join(' ');
  assert.doesNotMatch(joined, /不出设备|不會離開|never leaves/i);
  assert.match(joined, /伺服器|服务器|server/);
});

// ---------------------------------------------------------------------------
// The seam: the panel's adapter and the host adapter, loaded as the page loads
// ---------------------------------------------------------------------------

/**
 * The panel's action vocabulary, and the preload method each one means.
 *
 * Written out here rather than imported from either module: a test that read
 * the table it is checking would agree with any typo in it, which is exactly
 * the failure this section exists to catch.
 */
const PROJECT_ACTIONS = [
  ['projectOpenFile', 'open'],
  ['projectRevealFile', 'reveal'],
  ['projectCopyPath', 'copyPath'],
  ['projectSaveFileAs', 'saveAs'],
];

/**
 * `fork/desktop-host.js` and `fork/project-source.js` in one sandbox, over a
 * stub *preload* (`window.desktopHost`) that answers the way a container does.
 *
 * Both halves of this boundary are covered on their own -- `makeBridge` drives
 * the real IPC handlers, `loadAdapter` drives the real adapter -- and each test
 * supplies its own other side, so the two vocabularies were never compared
 * against each other. The panel names an action (`open`) while the host takes
 * the preload method (`projectOpenFile`), and a caller that names something the
 * host does not know is refused *by name*: silently, on every click, with the
 * bridge working perfectly on both sides. So the pair is built here in the
 * order the page builds it.
 */
function loadPageBridge(options = {}) {
  const calls = [];
  const declared = options.declared || PROJECT_ACTIONS.map(([method]) => method);
  const preload = {
    getCapabilities: () => Promise.resolve({
      bridge: options.bridge || '1.9.0',
      localFiles: true,
      methods: [
        'getCapabilities', 'suspendLocalContext', 'saveArtifact', 'openExternal', 'onHostEvent',
        'projectSource', 'projectTree', 'projectSearch', 'projectResolve', 'projectRead',
        'projectWrite', 'projectPreviewFile',
      ].concat(declared),
    }),
    suspendLocalContext: () => Promise.resolve({}),
    saveArtifact: () => Promise.resolve({}),
    openExternal: () => Promise.resolve({ opened: true }),
    onHostEvent: () => () => {},
    projectPreviewFile: (params) => {
      calls.push({ method: 'projectPreviewFile', params });
      return Promise.resolve({ name: 'summary.html', kind: 'html', size: 12, url: 'cow-preview://t/1' });
    },
  };
  for (const [method, action] of PROJECT_ACTIONS) {
    if (declared.indexOf(method) < 0) continue;
    preload[method] = (params) => {
      calls.push({ method, action, params });
      const answer = options.answers && options.answers[action];
      if (answer instanceof Error) return Promise.reject(answer);
      return Promise.resolve(answer || { ok: true, name: 'summary.csv' });
    };
  }
  const win = {
    console: { log() {}, warn() {}, error() {} },
    Promise, JSON, String, Number, Object, Array, Math, Date, RegExp, Error, isFinite,
    setTimeout, clearTimeout, encodeURIComponent, decodeURIComponent, URL,
    atob: (value) => Buffer.from(String(value), 'base64').toString('binary'),
    desktopHost: Object.assign(preload, options.preload || {}),
  };
  win.window = win;
  win.globalThis = win;
  const context = vm.createContext(win);
  vm.runInContext(hostAdapterJs, context, { filename: 'fork/desktop-host.js' });
  vm.runInContext(projectSourceJs, context, { filename: 'fork/project-source.js' });
  win.CowProjectSource.configure({ binding: () => ({ workspace_id: 'ws_1', binding_id: 'b_1' }) });
  return { host: win.CowDesktopHost, source: win.CowProjectSource, calls };
}

test('every action the panel names reaches the preload method the host declares', async () => {
  for (const [method, action] of PROJECT_ACTIONS) {
    const page = loadPageBridge();
    const reply = await page.source.act(action, 'out/summary.csv');
    assert.deepEqual(page.calls.map((call) => call.method), [method],
      `${action} must reach the shell as ${method}, not be refused on the way`);
    assert.equal(page.calls[0].params.workspace_id, 'ws_1', action);
    assert.equal(page.calls[0].params.path, 'out/summary.csv', action);
    assert.notEqual(reply.ok, false, `${action} is answered, not refused: ${JSON.stringify(reply)}`);
  }
});

test('the preview seam is wired the same way (task 9.5)', async () => {
  const page = loadPageBridge();
  const reply = await page.source.preview('reports/summary.html');
  assert.deepEqual(page.calls.map((call) => call.method), ['projectPreviewFile']);
  assert.equal(page.calls[0].params.path, 'reports/summary.html');
  assert.equal(reply.url, 'cow-preview://t/1', 'the page gets the embeddable URL back');
});

test('an action this host cannot carry out is refused by name, and the shell is never asked', async () => {
  const older = loadPageBridge({
    declared: ['projectOpenFile', 'projectRevealFile', 'projectCopyPath'],
  });
  assert.equal(await older.host.canUseProjectActions(), false,
    'an older host offers no action rather than half of them');
  const reply = await older.source.act('saveAs', 'a.csv');
  assert.equal(reply.ok, false);
  assert.equal(reply.code, 'feature_unavailable');
  assert.deepEqual(older.calls, [], 'the missing surface is never called');

  // The panel must also keep the answer the host itself gives: a refusal is an
  // answer, and it travels through unchanged.
  const refused = loadPageBridge({
    answers: { open: { ok: false, code: 'no_application', message: 'no application' } },
  });
  const answer = await refused.source.act('open', 'a.xlsx');
  assert.equal(answer.ok, false);
  assert.equal(answer.code, 'no_application');
  assert.equal(answer.message, 'no application');
});

test('an action name that is not one of the four is refused, table hooks included', async () => {
  const page = loadPageBridge();
  for (const bad of ['constructor', 'toString', 'hasOwnProperty', '__proto__', 'OPEN', '', null]) {
    const reply = await page.source.act(bad, 'a.txt');
    assert.equal(reply.ok, false, String(bad));
    assert.equal(reply.code, 'invalid_request', String(bad));
  }
  assert.deepEqual(page.calls, [], 'nothing invented reaches the shell');
});
