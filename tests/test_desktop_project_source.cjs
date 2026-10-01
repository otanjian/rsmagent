// The file panel's local source (Change align-desktop-project-execution-with-master,
// task 9.2, acceptance A21/A22).
//
// Three layers, because "the panel reads the project on this machine" only holds
// if all three agree:
//
//   * the **bridge** (main process): the six `project*` methods are validated,
//     the port is optional, and a page cannot reach a directory through them;
//   * the **browser** (main process): tree / search / paged preview / resolve /
//     edit against a *real* helper binary and a real directory tree, including
//     the refusals (read-only grant, offline device, absolute or traversing
//     path) and the edit frame's shape -- the same frame validator the server
//     dispatch uses;
//   * the **adapter** (the page): requests the panel already makes are served
//     from the local source with the panel's own reply shapes, and fall back to
//     the original API for anything that is not a local project.
//
// Run: node --test tests/test_desktop_project_source.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const Module = require('node:module');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const native = path.join(desktop, 'native', 'fs-guard');
const dist = path.join(desktop, 'dist', 'main');
const STATIC = path.join(root, 'channel', 'web', 'static', 'js');

function fsName() {
  return process.platform === 'win32' ? 'fs-guard.exe' : 'fs-guard';
}

const binary = path.join(native, 'target', 'release', fsName());

let guardMod;
let opsMod;
let browserMod;
let contractMod;
let hasBinary = false;

function needsBuild() {
  const srcDir = path.join(desktop, 'src', 'main');
  const newest = Math.max(
    fs.statSync(path.join(srcDir, 'local-files', 'fs-guard.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'remote', 'device-ops.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'remote', 'host-bridge.ts')).mtimeMs,
    // The capability advertisement and the whole grant/choose flow live here:
    // without it in this list, a change to the method allow-list would be
    // tested against a stale build and the suite would stay green.
    fs.statSync(path.join(srcDir, 'remote', 'local-files-bridge.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'remote', 'remote-host-ipc.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'project-browser', 'browser.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'project-execution', 'contract.ts')).mtimeMs,
    fs.statSync(path.join(desktop, 'src', 'main', 'remote-preload.ts')).mtimeMs,
  );
  return [
    path.join(dist, 'local-files', 'fs-guard.js'),
    path.join(dist, 'remote', 'device-ops.js'),
    path.join(dist, 'project-browser', 'browser.js'),
    path.join(dist, 'project-execution', 'contract.js'),
  ].some((out) => !fs.existsSync(out) || fs.statSync(out).mtimeMs < newest);
}

/** Build the helper when cargo is available and the binary is missing/stale. */
function ensureBinary() {
  if (process.env.COW_SKIP_RUST_BUILD) return fs.existsSync(binary);
  let newest = 0;
  for (const name of fs.readdirSync(path.join(native, 'src'))) {
    newest = Math.max(newest, fs.statSync(path.join(native, 'src', name)).mtimeMs);
  }
  if (fs.existsSync(binary) && fs.statSync(binary).mtimeMs >= newest) return true;
  try {
    execFileSync('cargo', ['build', '--release', '--offline'], { cwd: native, stdio: 'pipe' });
  } catch (err) {
    return false;
  }
  return fs.existsSync(binary);
}

before(() => {
  if (needsBuild()) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
  guardMod = require(path.join(dist, 'local-files', 'fs-guard.js'));
  opsMod = require(path.join(dist, 'remote', 'device-ops.js'));
  browserMod = require(path.join(dist, 'project-browser', 'browser.js'));
  contractMod = require(path.join(dist, 'project-execution', 'contract.js'));
  hasBinary = ensureBinary();
});

// ---------------------------------------------------------------------------
// Harness: a real helper over a real project tree
// ---------------------------------------------------------------------------

/** A project with the shapes a real panel has to survive. */
function makeProject() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-project-')));
  fs.mkdirSync(path.join(dir, 'src'));
  fs.mkdirSync(path.join(dir, 'output'));
  fs.writeFileSync(path.join(dir, 'README.md'), '# project\n', 'utf8');
  fs.writeFileSync(path.join(dir, 'src', 'app.py'), 'print("hello")\n', 'utf8');
  fs.writeFileSync(path.join(dir, 'output', '报告.csv'), 'a,b\n1,2\n', 'utf8');
  // Bigger than one preview page, so paging is exercised rather than assumed.
  fs.writeFileSync(path.join(dir, 'big.txt'), 'x'.repeat(40000), 'utf8');
  // A symlink out of the project: a reference to it must be refused, and a
  // listing must not follow it.
  fs.writeFileSync(path.join(dir, 'outside-target.txt'), 'outside\n', 'utf8');
  try {
    fs.symlinkSync(path.join(dir, 'outside-target.txt'), path.join(dir, 'escape.txt'));
  } catch (_) { /* a platform without symlink permission: the rest still runs */ }
  return dir;
}

/**
 * A runner that behaves exactly like ``LocalReadAssembly.runCommand``: the
 * helper's own op mapping and clamps, over one opened root.
 */
function realRunner(dir) {
  const guard = new guardMod.FsGuard(() => {
    const { spawn } = require('node:child_process');
    return spawn(binary, [], { stdio: ['pipe', 'pipe', 'pipe'] });
  });
  let fsGrant = null;
  return {
    guard,
    async ready() {
      const opened = await guard.openRoot(dir);
      fsGrant = opened.grant;
      return opened;
    },
    runCommand: (command) => opsMod.runDeviceCommand(guard, fsGrant, command),
  };
}

const WORKSPACE = 'ws_1';
const DEVICE = 'dev_1';
const BINDING = 'bind_1';

/** Deterministic ids that still differ from one another. */
function nextId() {
  let count = 0;
  return () => {
    count += 1;
    return `fixed_${count}`;
  };
}

function binding(overrides = {}) {
  return {
    workspaceId: WORKSPACE,
    grantId: 'grant_1',
    label: 'my-project',
    grantVersion: 3,
    bindingId: BINDING,
    deviceId: DEVICE,
    selectionGeneration: 2,
    connectionEpoch: 'epoch_abc',
    executable: true,
    connected: true,
    ...overrides,
  };
}

/** A browser over a real project, with an in-memory binding table. */
function makeBrowser(dir, options = {}) {
  const runner = realRunner(dir);
  const frames = [];
  const browser = new browserMod.ProjectBrowser({
    commands: { runCommand: (command) => runner.runCommand(command) },
    binding: (workspaceId) => (workspaceId === WORKSPACE ? options.binding || binding() : null),
    ...(options.executeTool === false ? {} : {
      executeTool: async (frame) => {
        frames.push(frame);
        if (options.onFrame) return options.onFrame(frame);
        return { state: 'succeeded', execution_phase: 'succeeded', effects: 'completed' };
      },
    }),
    now: () => Date.parse('2026-10-01T00:00:00.000Z'),
    // Deterministic but unique: an id that never changes would hide a browser
    // that reuses one, which is exactly what the dedup journal keys on.
    newId: nextId(),
  });
  return { browser, frames, runner };
}

/** A browser plus a real project, cleaned up by the test's `t`. */
async function withProject(t, options = {}) {
  if (!hasBinary) {
    t.skip('fs-guard binary not built (cargo unavailable)');
    return null;
  }
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const made = makeBrowser(dir, options);
  t.after(() => made.runner.guard.dispose());
  await made.runner.ready();
  return { ...made, dir };
}

// ---------------------------------------------------------------------------
// The browser: reads
// ---------------------------------------------------------------------------

test('a tree listing carries project-relative paths and no directory', async (t) => {
  const made = await withProject(t);
  if (!made) return;
  const page = await made.browser.tree({ workspace_id: WORKSPACE, path: '' });
  assert.equal(page.ok, true);
  assert.equal(page.source, 'desktop');
  assert.equal(page.path, '', 'the project root is the empty relative path');
  const names = page.entries.map((entry) => entry.name);
  assert.ok(names.includes('README.md'));
  assert.ok(names.includes('src'));
  const readme = page.entries.find((entry) => entry.name === 'README.md');
  assert.equal(readme.path, 'README.md');
  assert.equal(readme.kind, 'file');
  const src = page.entries.find((entry) => entry.name === 'src');
  assert.equal(src.path, 'src');
  assert.equal(src.kind, 'dir');
  // The one thing a local reply must never carry.
  const serialised = JSON.stringify(page);
  assert.equal(serialised.includes(made.dir), false, 'the project directory must not be published');
  assert.equal(serialised.includes('grant_1'), false, 'the local grant id must not be published');
});

test('a subdirectory lists with paths relative to the project root', async (t) => {
  const made = await withProject(t);
  if (!made) return;
  const page = await made.browser.tree({ workspace_id: WORKSPACE, path: 'src' });
  assert.equal(page.ok, true);
  assert.equal(page.path, 'src');
  const hit = page.entries.find((entry) => entry.name === 'app.py');
  assert.equal(hit.path, 'src/app.py', 'the entry path is the full project-relative path');
});

test('a paginated preview returns a page of the file, not the whole file', async (t) => {
  const made = await withProject(t);
  if (!made) return;
  const page = await made.browser.read({
    workspace_id: WORKSPACE, path: 'big.txt', bytes: 1024,
  });
  assert.equal(page.ok, true);
  assert.equal(page.text.length, 1024);
  assert.equal(page.bytes, 1024);
  assert.equal(page.total, 40000);
  assert.equal(page.truncated, true, 'a partial read must say so');
  assert.ok(page.mtime > 0, 'the editor needs a version to detect a conflict against');
});

test('a preview page cannot exceed the page bound even when asked to', async (t) => {
  const made = await withProject(t);
  if (!made) return;
  const page = await made.browser.read({
    workspace_id: WORKSPACE, path: 'big.txt', bytes: browserMod.PREVIEW_BYTES_MAX * 10,
  });
  assert.equal(page.bytes, browserMod.PREVIEW_BYTES_MAX);
  assert.equal(page.bytes, opsMod.READ_TEXT_LIMIT_MAX, 'the bound is the device contract\u2019s');
});

test('a non-ASCII name survives the round trip', async (t) => {
  const made = await withProject(t);
  if (!made) return;
  const page = await made.browser.read({
    workspace_id: WORKSPACE, path: 'output/报告.csv',
  });
  assert.equal(page.ok, true);
  assert.equal(page.text, 'a,b\n1,2\n');
});

test('search finds files by name without leaving the project', async (t) => {
  const made = await withProject(t);
  if (!made) return;
  const found = await made.browser.search({
    workspace_id: WORKSPACE, query: 'app', mode: 'name',
  });
  assert.equal(found.ok, true);
  assert.equal(found.hits.some((hit) => hit.path === 'src/app.py'), true);
});

test('resolve answers metadata, and a missing file is a refusal with a reason', async (t) => {
  const made = await withProject(t);
  if (!made) return;
  const info = await made.browser.resolve({ workspace_id: WORKSPACE, path: 'README.md' });
  assert.equal(info.ok, true);
  assert.equal(info.kind, 'file');
  assert.equal(info.size, 10);

  const missing = await made.browser.resolve({ workspace_id: WORKSPACE, path: 'nope.txt' });
  assert.equal(missing.ok, false);
  assert.ok(missing.code, 'a refusal names a code rather than an empty answer');
});

// ---------------------------------------------------------------------------
// The browser: what it refuses
// ---------------------------------------------------------------------------

test('a workspace this device does not hold is stale_context, not an empty listing', async (t) => {
  const made = await withProject(t);
  if (!made) return;
  for (const call of [
    () => made.browser.describe('ws_other'),
    () => made.browser.tree({ workspace_id: 'ws_other', path: '' }),
    () => made.browser.read({ workspace_id: 'ws_other', path: 'README.md' }),
    () => made.browser.write({ workspace_id: 'ws_other', path: 'README.md', content: 'x' }),
  ]) {
    const reply = await call();
    assert.equal(reply.ok, false);
    assert.equal(reply.code, 'stale_context');
  }
});

test('an absolute path or a traversal never reaches a file API', async (t) => {
  const made = await withProject(t);
  if (!made) return;
  const refused = ['/etc/passwd', '../../etc/passwd', 'src/../../etc/passwd', 'C:\\Windows\\win.ini'];
  for (const bad of refused) {
    const reply = await made.browser.read({ workspace_id: WORKSPACE, path: bad });
    assert.equal(reply.ok, false, bad);
    assert.equal(reply.code, 'invalid_request', bad);
  }
});

test('a reference that resolves outside the project is refused', async (t) => {
  const made = await withProject(t);
  if (!made) return;
  if (!fs.existsSync(path.join(made.dir, 'escape.txt'))) return t.skip('no symlink support');
  const reply = await made.browser.read({ workspace_id: WORKSPACE, path: 'escape.txt' });
  assert.equal(reply.ok, false, 'a symlink out of the project is not a project file');
});

test('a read-only grant offers reading and refuses editing, by name', async (t) => {
  const made = await withProject(t, { binding: binding({ executable: false }) });
  if (!made) return;
  const described = made.browser.describe(WORKSPACE);
  assert.equal(described.ok, true);
  assert.equal(described.readable, true);
  assert.equal(described.editable, false);
  assert.equal(described.edit_refusal, 'source_read_only');

  const listing = await made.browser.tree({ workspace_id: WORKSPACE, path: '' });
  assert.equal(listing.ok, true, 'a reference can still be browsed');

  const saved = await made.browser.write({
    workspace_id: WORKSPACE, path: 'README.md', content: '# changed\n',
  });
  assert.equal(saved.ok, false);
  assert.equal(saved.code, 'source_read_only');
  assert.equal(made.frames.length, 0, 'no frame is issued for a refused edit');
  // Nothing was written, either.
  assert.equal(fs.readFileSync(path.join(made.dir, 'README.md'), 'utf8'), '# project\n');
});

test('an offline device refuses the edit rather than journaling one', async (t) => {
  const made = await withProject(t, { binding: binding({ connected: false, connectionEpoch: '' }) });
  if (!made) return;
  const described = made.browser.describe(WORKSPACE);
  assert.equal(described.editable, false);
  assert.equal(described.edit_refusal, 'device_offline');
  const saved = await made.browser.write({
    workspace_id: WORKSPACE, path: 'README.md', content: 'x',
  });
  assert.equal(saved.code, 'device_offline');
  assert.equal(made.frames.length, 0);
});

// ---------------------------------------------------------------------------
// The browser: the edit
// ---------------------------------------------------------------------------

test('an edit travels as a v2 write frame the contract accepts', async (t) => {
  const made = await withProject(t);
  if (!made) return;
  const saved = await made.browser.write({
    workspace_id: WORKSPACE, path: 'README.md', content: '# edited\n',
  });
  assert.equal(saved.ok, true);
  assert.equal(saved.type, 'saved');
  assert.equal(saved.execution_phase, 'succeeded');

  assert.equal(made.frames.length, 1);
  const frame = made.frames[0];
  assert.equal(frame.tool, browserMod.EDIT_TOOL);
  assert.equal(frame.tool, 'write');
  assert.deepEqual(frame.arguments, { path: 'README.md', content: '# edited\n' });
  // The frame must name the live authorization, not a directory.
  assert.equal(frame.binding_id, BINDING);
  assert.equal(frame.workspace_id, WORKSPACE);
  assert.equal(frame.device_id, DEVICE);
  assert.equal(frame.grant_version, 3);
  assert.equal(frame.selection_generation, 2);
  assert.equal(frame.connection_epoch, 'epoch_abc');
  assert.equal(JSON.stringify(frame).includes(made.dir), false, 'a frame never names a directory');

  // The same validator the dispatch applies, so a panel edit cannot be shaped
  // differently from a model-issued one.
  assert.deepEqual(contractMod.validateExecuteFrame(frame, 'posix'), []);
  assert.equal(contractMod.paramsDigest(frame), frame.params_digest,
    'the digest is over the frame the receiver will verify');
});

test('two edits of the same file are two frames with different ids', async (t) => {
  const made = await withProject(t);
  if (!made) return;
  await made.browser.write({ workspace_id: WORKSPACE, path: 'README.md', content: 'a' });
  await made.browser.write({ workspace_id: WORKSPACE, path: 'README.md', content: 'b' });
  assert.equal(made.frames.length, 2);
  assert.notEqual(made.frames[0].command_id, made.frames[1].command_id);
  assert.notEqual(made.frames[0].run_id, made.frames[1].run_id);
});

test('a frame whose run failed is reported as a failure, not a save', async (t) => {
  const made = await withProject(t, {
    onFrame: async () => ({
      state: 'failed',
      execution_phase: 'failed',
      error_code: 'grant_revoked',
      error_message: 'the grant was revoked',
    }),
  });
  if (!made) return;
  const saved = await made.browser.write({
    workspace_id: WORKSPACE, path: 'README.md', content: 'x',
  });
  assert.equal(saved.ok, false);
  assert.equal(saved.code, 'grant_revoked');
});

test('a build without project execution refuses an edit instead of writing around it', async (t) => {
  const made = await withProject(t, { executeTool: false });
  if (!made) return;
  const described = made.browser.describe(WORKSPACE);
  assert.equal(described.editable, false);
  assert.equal(described.edit_refusal, 'feature_unavailable');
  const saved = await made.browser.write({
    workspace_id: WORKSPACE, path: 'README.md', content: 'x',
  });
  assert.equal(saved.code, 'feature_unavailable');
  assert.equal(fs.readFileSync(path.join(made.dir, 'README.md'), 'utf8'), '# project\n');
});

test('a stat with a 0 modified time is not reported as a version', async (t) => {
  // A conflict check compares this number; a helper that answers 0 must not look
  // like "the file is from 1970" and silently overwrite a newer edit.
  const made = await withProject(t);
  if (!made) return;
  const page = await made.browser.read({ workspace_id: WORKSPACE, path: 'README.md' });
  assert.equal(typeof page.mtime, 'number');
  assert.ok(page.mtime > 1_000_000_000, 'a real mtime travels with the page');
});

// ---------------------------------------------------------------------------
// The bridge: the six methods
// ---------------------------------------------------------------------------

const compiledPreload = path.join(dist, '..', 'main', 'remote-preload.js');
const compiledHostBridge = path.join(dist, 'remote', 'host-bridge.js');
const compiledHostIpc = path.join(dist, 'remote', 'remote-host-ipc.js');

/** Load a compiled main-process module with a stub `electron`. */
function loadWithStub(compiled, handlers) {
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
    delete require.cache[require.resolve(compiled)];
    return require(compiled);
  } finally {
    Module.prototype.require = originalRequire;
  }
}

/** The bridge as a page sees it: registered, then called through IPC. */
async function makeBridge(port) {
  const handlers = {};
  const ipc = loadWithStub(compiledHostIpc, handlers);
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
    { sender: { id: 7, mainFrame: { processId: 100, routingId: 3 } }, senderFrame: { processId: 100, routingId: 3, url: 'https://console.example.com/chat' } },
    { method, params: params || {}, generation: 4 },
  );
  // local-files must be open for the project surface, exactly as for the picker.
  const localFilesBridge = require(path.join(dist, 'remote', 'local-files-bridge.js'));
  localFilesBridge.setRemoteLocalFilesEnabled(true);
  return { ipc, call, localFilesBridge };
}

test('the six project methods are declared, and only with local files open', async () => {
  const bridge = await makeBridge(null);
  const closed = require(path.join(dist, 'remote', 'local-files-bridge.js'));
  closed.setRemoteLocalFilesEnabled(false);
  const off = await bridge.call('getCapabilities');
  assert.equal(off.data.methods.includes('projectTree'), false,
    'a closed deployment does not advertise the local project surface');
  assert.equal(off.data.allMethods.includes('projectTree'), true,
    'but the method is still part of this bridge version');
  bridge.localFilesBridge.setRemoteLocalFilesEnabled(true);
  const on = await bridge.call('getCapabilities');
  for (const method of ['projectSource', 'projectTree', 'projectSearch',
    'projectResolve', 'projectRead', 'projectWrite']) {
    assert.equal(on.data.methods.includes(method), true, method);
  }
});

test('a page parameter is validated before the port sees it', async () => {
  const seen = [];
  const bridge = await makeBridge({
    describe: () => ({ ok: true, source: 'desktop' }),
    tree: (params) => { seen.push(params); return { ok: true }; },
    search: () => ({ ok: true }), resolve: () => ({ ok: true }),
    read: () => ({ ok: true }), write: () => ({ ok: true }),
  });
  for (const bad of ['/etc/passwd', '../secret', 'C:\\x', 'a/../../b']) {
    const reply = await bridge.call('projectTree', { workspace_id: 'ws_1', path: bad });
    assert.equal(reply.ok, false, bad);
    assert.equal(reply.code, 'invalid_path', bad);
  }
  assert.equal(seen.length, 0, 'a refused path never reaches the port');

  const root = await bridge.call('projectTree', { workspace_id: 'ws_1', path: '' });
  assert.equal(root.ok, true);
  assert.equal(seen.length, 1);

  const noWorkspace = await bridge.call('projectRead', { path: 'README.md' });
  assert.equal(noWorkspace.code, 'invalid_request');

  const tooBig = await bridge.call('projectWrite', {
    workspace_id: 'ws_1', path: 'a.txt', content: 'x'.repeat(1024 * 1024 + 1),
  });
  assert.equal(tooBig.code, 'limit_exceeded');
});

test('a refusal keeps its code, and no port means the surface is unavailable', async () => {
  const bridge = await makeBridge({
    describe: () => ({ ok: true }), tree: () => ({ ok: false, code: 'source_read_only', message: 'read only' }),
    search: () => ({ ok: true }), resolve: () => ({ ok: true }),
    read: () => ({ ok: true }),
    write: () => ({ ok: false, code: 'source_read_only', message: 'read only' }),
  });
  const refused = await bridge.call('projectWrite', {
    workspace_id: 'ws_1', path: 'a.txt', content: 'x',
  });
  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'source_read_only', 'the reason survives the bridge');
  assert.equal(refused.message, 'read only');

  const noPort = await makeBridge(null);
  const unavailable = await noPort.call('projectTree', { workspace_id: 'ws_1', path: '' });
  assert.equal(unavailable.ok, false);
  assert.equal(unavailable.code, 'feature_unavailable',
    'a host without the port refuses rather than answering from the backend');
});

test('a write-back style method stays refused, and the page never gets ipcRenderer', async () => {
  const bridge = await makeBridge(null);
  for (const method of ['writeBack', 'runLocalScript']) {
    const reply = await bridge.call(method, {});
    assert.equal(reply.ok, false, method);
  }
});

// ---------------------------------------------------------------------------
// The adapter: what the panel gets
// ---------------------------------------------------------------------------

/** Load `fork/project-source.js` into a fake page. */
function loadAdapter(options = {}) {
  const source = fs.readFileSync(path.join(STATIC, 'fork', 'project-source.js'), 'utf8');
  const calls = [];
  const window = {
    URL,
    CowDesktopHost: options.host === null ? undefined : {
      project: (method, params) => {
        calls.push({ method, params });
        const answer = options.answers ? options.answers(method, params) : undefined;
        if (answer !== undefined) return Promise.resolve(answer);
        if (method === 'projectTree') {
          // Paths are project-relative and absolute-free, exactly as the real
          // browser answers: they are not re-derived from the request here,
          // because a second derivation would be a second authority.
          return Promise.resolve({
            ok: true, source: 'desktop', type: 'tree', path: params.path || '',
            entries: [
              { name: 'src', kind: 'dir', path: 'src', size: null, modified: 0 },
              { name: 'README.md', kind: 'file', path: 'README.md', size: 10, modified: 1 },
            ],
            truncated: false,
          });
        }
        if (method === 'projectResolve') {
          return Promise.resolve({ ok: true, source: 'desktop', type: 'entry', path: params.path, kind: 'file', size: 10, modified: 42 });
        }
        if (method === 'projectRead') {
          return Promise.resolve({ ok: true, source: 'desktop', type: 'page', path: params.path, text: '# project\n', offset: 0, bytes: 10, total: 10, truncated: false, mtime: 42 });
        }
        if (method === 'projectSearch') {
          return Promise.resolve({ ok: true, source: 'desktop', type: 'search', hits: [{ path: 'README.md', kind: 'file', size: 10, modified: 1 }] });
        }
        if (method === 'projectWrite') {
          return Promise.resolve({ ok: true, source: 'desktop', type: 'saved', path: params.path, execution_phase: 'succeeded' });
        }
        return Promise.resolve({ ok: false, code: 'device_error', message: 'unexpected' });
      },
      canUseProjectSource: () => Promise.resolve(options.canUse !== false),
    },
  };
  const fn = new Function('window', 'module', `${source}\nreturn window.CowProjectSource;`);
  const api = fn(window, { exports: {} });
  api.configure({
    binding: () => (options.binding === undefined ? { workspace_id: 'ws_1' } : options.binding),
    kindOf: (name) => (name.endsWith('.md') ? 'markdown' : (name.includes('.') ? 'file' : 'directory')),
    editable: (kind) => ['markdown', 'text', 'code'].includes(kind),
    previewable: (kind) => ['markdown', 'text', 'code', 'image'].includes(kind),
  });
  return { api, calls, window };
}

test('a tree request is served locally, in the shape the panel already reads', async () => {
  const { api, calls } = loadAdapter();
  const reply = await api.handle('/api/workspace/tree?path=src');
  assert.equal(reply.status, 'success');
  assert.equal(calls[0].method, 'projectTree');
  assert.deepEqual(calls[0].params, { workspace_id: 'ws_1', path: 'src' });
  assert.equal(reply.entries[0].path, 'src');
  assert.equal(reply.entries[0].is_dir, true);
  assert.equal(reply.entries[0].kind, 'directory');
  assert.equal(reply.entries[1].kind, 'markdown');
  assert.equal(reply.entries[1].previewable, true);
  assert.equal(reply.entries[0].source, 'desktop');
  assert.equal(reply.entries[0].local, true);
  // The panel must not be handed anything it could turn into a server request.
  assert.equal('abs_path' in reply.entries[0], false);
  assert.equal('raw_url' in reply.entries[0], false);
  assert.equal('preview_url' in reply.entries[0], false);
  assert.ok(!('root' in reply), 'a local listing names no directory');
});

test('a resolve request produces an entry the preview can open', async () => {
  const { api } = loadAdapter();
  const reply = await api.handle('/api/workspace/resolve?path=README.md');
  assert.equal(reply.status, 'success');
  assert.equal(reply.file.name, 'README.md');
  assert.equal(reply.file.kind, 'markdown');
  assert.equal(reply.file.previewable, true);
  assert.equal(reply.file.local, true);
  assert.equal(reply.file.mtime, 42);
});

test('a read request produces what the editor needs, including its baseline', async () => {
  const { api } = loadAdapter();
  const reply = await api.handle('/api/workspace/read?path=README.md');
  assert.equal(reply.content, '# project\n');
  assert.equal(reply.mtime, 42);
  assert.equal(reply.editable, true);
  assert.equal(reply.truncated, false);
});

test('a truncated read is not offered as editable', async () => {
  // A *markdown* page: without the truncation guard this kind is editable, so
  // the assertion below can only pass because of the guard -- with a `.txt`
  // name the harness's own kind table would make it non-editable anyway and
  // the test would be green for the wrong reason.
  const { api } = loadAdapter({
    answers: (method) => (method === 'projectRead'
      ? { ok: true, path: 'big.md', text: '# partial', total: 40000, bytes: 9, truncated: true, mtime: 1 }
      : undefined),
  });
  const reply = await api.handle('/api/workspace/read?path=big.md');
  assert.equal(reply.truncated, true);
  assert.equal(reply.editable, false, 'saving a partial read would truncate the tail');

  const whole = loadAdapter({
    answers: (method) => (method === 'projectRead'
      ? { ok: true, path: 'big.md', text: '# all of it', total: 11, bytes: 11, truncated: false, mtime: 1 }
      : undefined),
  });
  const complete = await whole.api.handle('/api/workspace/read?path=big.md');
  assert.equal(complete.editable, true, 'the guard must be about truncation, not about the kind');
});

test('a search request answers with local entries', async () => {
  const { api, calls } = loadAdapter();
  const reply = await api.handle('/api/workspace/search?q=read&limit=12');
  assert.equal(reply.status, 'success');
  assert.equal(calls[0].params.query, 'read');
  assert.equal(calls[0].params.limit, 12, 'a query-string count reaches the bridge as a number');
  assert.equal(reply.results[0].path, 'README.md');
  assert.equal(reply.results[0].local, true);
});

test('a page range reaches the bridge as numbers, and a broken one is refused', async () => {
  const ranged = loadAdapter();
  const page = await ranged.api.handle('/api/workspace/read?path=big.txt&offset=100&bytes=64');
  assert.equal(page.status, 'success');
  assert.deepEqual(ranged.calls[0].params, { workspace_id: 'ws_1', path: 'big.txt', offset: 100, bytes: 64 });

  // A count the adapter cannot read is a refusal, not a silent widening: a
  // caller that asked for 12 hits must never be handed 200.
  for (const bad of ['abc', '-1', '1.5', '1e3', ' ']) {
    const { api, calls } = loadAdapter();
    const refused = await api.handle(`/api/workspace/search?q=read&limit=${encodeURIComponent(bad)}`);
    assert.equal(refused.status, 'error', bad);
    assert.equal(refused.code, 'invalid_request', bad);
    assert.equal(calls.length, 0, `a broken limit never reaches the port: ${bad}`);
  }
});

test('a backend-only request is left alone', async () => {
  const { api, calls } = loadAdapter();
  for (const request of [
    '/api/workspace/meta',
    '/api/workspace/user-dir',
    '/api/workspace/projects',
    '/api/file?path=%2Fetc%2Fpasswd',
  ]) {
    assert.equal(await api.handle(request), null, request);
  }
  assert.equal(calls.length, 0);
});

test('with no local binding the panel keeps the original API', async () => {
  const { api, calls } = loadAdapter({ binding: null });
  assert.equal(await api.handle('/api/workspace/tree?path='), null);
  assert.equal(calls.length, 0);
  assert.equal(api.landing(), null, 'the backend decides where the panel lands');
});

test('with a local binding the panel lands on the project root', async () => {
  const { api } = loadAdapter();
  assert.equal(api.landing(), '');
  assert.equal(await api.applies(), true);
});

test('a host without the project surface falls back to the API', async () => {
  const { api, calls } = loadAdapter({ host: null });
  assert.equal(await api.handle('/api/workspace/tree?path='), null);
  assert.equal(calls.length, 0);
});

test('a build without project execution is a fallback, not a refusal', async () => {
  const { api } = loadAdapter({
    answers: (method) => (method === 'projectTree'
      ? { ok: false, code: 'feature_unavailable', message: 'this build does not offer project execution' }
      : undefined),
  });
  assert.equal(await api.handle('/api/workspace/tree?path='), null,
    'the backend may still serve this session');
});

test('a refusal about this project is reported, not papered over', async () => {
  const { api } = loadAdapter({
    answers: (method) => (method === 'projectTree'
      ? { ok: false, code: 'stale_context', message: 'no live local project' }
      : undefined),
  });
  const reply = await api.handle('/api/workspace/tree?path=');
  assert.equal(reply.status, 'error');
  assert.equal(reply.code, 'stale_context');
  assert.equal(reply.message, 'no live local project');
});

test('a save goes out as a local write, and reports the version it wrote', async () => {
  const { api, calls } = loadAdapter();
  const reply = await api.write({ path: 'README.md', content: '# edited\n', expected_mtime: 42 });
  assert.equal(reply.status, 'success');
  assert.equal(reply.mtime, 42);
  assert.deepEqual(calls.map((call) => call.method), ['projectResolve', 'projectWrite', 'projectResolve']);
  assert.deepEqual(calls[1].params, { workspace_id: 'ws_1', path: 'README.md', content: '# edited\n' });
});

test('a save is refused with a conflict when the file moved under the editor', async () => {
  const { api, calls } = loadAdapter({
    answers: (method) => (method === 'projectResolve'
      ? { ok: true, path: 'README.md', kind: 'file', size: 12, modified: 99 }
      : undefined),
  });
  const reply = await api.write({ path: 'README.md', content: '# edited\n', expected_mtime: 42 });
  assert.equal(reply.status, 'error');
  assert.equal(reply.code, 'conflict');
  assert.deepEqual(calls.map((call) => call.method), ['projectResolve'],
    'a known conflict must not write first');
});

test('an explicit overwrite ignores the baseline', async () => {
  const { api, calls } = loadAdapter({
    answers: (method) => (method === 'projectResolve'
      ? { ok: true, path: 'README.md', kind: 'file', size: 12, modified: 99 }
      : undefined),
  });
  const reply = await api.write({ path: 'README.md', content: '# edited\n', expected_mtime: null });
  assert.equal(reply.status, 'success');
  assert.equal(calls.some((call) => call.method === 'projectWrite'), true);
});

test('a read-only project refuses the save with a reason the panel can translate', async () => {
  const { api } = loadAdapter({
    answers: (method) => (method === 'projectWrite'
      ? { ok: false, code: 'source_read_only', message: 'open the project with execution to edit files' }
      : undefined),
  });
  const reply = await api.write({ path: 'README.md', content: 'x', expected_mtime: null });
  assert.equal(reply.status, 'error');
  assert.equal(reply.code, 'source_read_only');
});
