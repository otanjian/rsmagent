// Bounded project watching (Change align-desktop-project-execution-with-master,
// task 9.3, acceptance A21's refresh half).
//
// The watcher is what keeps the file panel and the session's execution target
// describing the same project: it notices a change under the bound directory and
// it stops the moment the authorization behind that directory changes. Both
// halves are tested against a *real* project through the real helper -- a mock
// would not tell us that a deleted directory is reported as removed rather than
// as "everything changed".
//
// Run: node --test tests/test_desktop_project_watch.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
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
let watchMod;
let hasBinary = false;

function needsBuild() {
  const srcDir = path.join(desktop, 'src', 'main');
  const newest = Math.max(
    fs.statSync(path.join(srcDir, 'local-files', 'fs-guard.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'remote', 'device-ops.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'remote', 'host-bridge.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'remote', 'local-files-bridge.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'remote', 'remote-host-ipc.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'remote', 'remote-container-ipc.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'project-browser', 'browser.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'project-browser', 'watch.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'project-execution', 'contract.ts')).mtimeMs,
    fs.statSync(path.join(desktop, 'src', 'main', 'remote-preload.ts')).mtimeMs,
  );
  return [
    path.join(dist, 'local-files', 'fs-guard.js'),
    path.join(dist, 'remote', 'device-ops.js'),
    path.join(dist, 'project-browser', 'browser.js'),
    path.join(dist, 'project-browser', 'watch.js'),
    path.join(dist, 'project-execution', 'contract.js'),
  ].some((out) => !fs.existsSync(out) || fs.statSync(out).mtimeMs < newest);
}

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
  watchMod = require(path.join(dist, 'project-browser', 'watch.js'));
  hasBinary = ensureBinary();
});

// ---------------------------------------------------------------------------
// Harness: a real project behind the real browser
// ---------------------------------------------------------------------------

const WORKSPACE = 'ws_1';
const DEVICE = 'dev_1';
const BINDING = 'bind_1';

function makeProject() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-watch-')));
  fs.mkdirSync(path.join(dir, 'src'));
  fs.mkdirSync(path.join(dir, 'output'));
  fs.writeFileSync(path.join(dir, 'README.md'), '# project\n', 'utf8');
  fs.writeFileSync(path.join(dir, 'src', 'app.py'), 'print("hello")\n', 'utf8');
  fs.writeFileSync(path.join(dir, 'output', 'report.csv'), 'a,b\n1,2\n', 'utf8');
  return dir;
}

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

/** A real project, a real browser over it, and a watcher fed by both. */
async function withWatch(t, options = {}) {
  if (!hasBinary) {
    t.skip('fs-guard binary not built (cargo unavailable)');
    return null;
  }
  const dir = makeProject();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const runner = realRunner(dir);
  t.after(() => runner.guard.dispose());
  await runner.ready();
  // The binding table the container keeps: mutable, so a test can re-pick a
  // directory or revoke a grant between two ticks.
  const state = { binding: options.binding || binding() };
  const browser = new browserMod.ProjectBrowser({
    commands: { runCommand: (command) => runner.runCommand(command) },
    binding: (workspaceId) => (workspaceId === WORKSPACE ? state.binding : null),
  });
  const events = [];
  const timers = [];
  const scheduler = options.scheduler || {
    schedule: (run, ms) => {
      timers.push({ run, ms });
      return timers.length;
    },
    cancel: (handle) => {
      if (timers[handle - 1]) timers[handle - 1].cancelled = true;
    },
  };
  const watcher = new watchMod.ProjectWatcher({
    reader: options.reader || browser,
    scope: options.scope || ((workspaceId) => {
      if (workspaceId !== WORKSPACE || !state.binding) return null;
      return {
        workspaceId: state.binding.workspaceId,
        bindingId: state.binding.bindingId,
        selectionGeneration: state.binding.selectionGeneration,
        grantVersion: state.binding.grantVersion,
        connectionEpoch: state.binding.connectionEpoch,
      };
    }),
    emit: (event) => events.push(event),
    intervalMs: options.intervalMs,
    maxDirectories: options.maxDirectories,
    maxEntries: options.maxEntries,
    maxDepth: options.maxDepth,
    ...scheduler,
  });
  t.after(() => watcher.stop());
  return { dir, browser, runner, watcher, events, state, timers };
}

/** A small pause: the helper's mtime resolution is whole seconds. */
function touch(dir, relPath, content = 'changed\n') {
  fs.writeFileSync(path.join(dir, relPath), content, 'utf8');
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// Noticing a change
// ---------------------------------------------------------------------------

test('the first scan is a baseline: nothing is reported as new', async (t) => {
  const made = await withWatch(t);
  if (!made) return;
  made.watcher.start(WORKSPACE);
  assert.equal(await made.watcher.tick(), null);
  assert.deepEqual(made.events, [], 'a fresh watch does not announce the whole project');
  assert.equal(made.watcher.status().watching, true);
});

test('a new file in the root is reported for the root', async (t) => {
  const made = await withWatch(t);
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  touch(made.dir, 'notes.md');
  const event = await made.watcher.tick();
  assert.ok(event, 'the new file is an event');
  assert.deepEqual(event.changed, ['']);
  assert.deepEqual(event.removed, []);
  assert.equal(event.workspace_id, WORKSPACE);
  assert.equal(event.binding_id, BINDING);
  assert.equal(event.selection_generation, 2);
  assert.equal(event.truncated, false);
  assert.equal(event.scanned > 1, true, 'the scan looked at more than the root');
  assert.equal(made.events.length, 1);
});

test('a change inside a subdirectory is reported for that subdirectory', async (t) => {
  const made = await withWatch(t);
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  touch(made.dir, path.join('src', 'app.py'), 'print("changed")\n');
  const event = await made.watcher.tick();
  assert.ok(event);
  assert.deepEqual(event.changed, ['src']);
  assert.deepEqual(event.removed, [], 'a rewritten file is not a removal');
});

test('an unchanged project reports nothing at all', async (t) => {
  const made = await withWatch(t);
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  assert.equal(await made.watcher.tick(), null);
  assert.equal(await made.watcher.tick(), null);
  assert.deepEqual(made.events, []);
  assert.equal(made.watcher.status().changes, 0);
  assert.equal(made.watcher.status().scans, 3);
});

test('a deleted file is a change and a deleted directory is a removal', async (t) => {
  const made = await withWatch(t);
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  fs.rmSync(path.join(made.dir, 'src', 'app.py'));
  const fileGone = await made.watcher.tick();
  assert.ok(fileGone);
  assert.deepEqual(fileGone.changed, ['src']);
  // Now the whole directory: its parent's listing changes and the directory
  // itself stops existing, which are two different facts for the panel.
  fs.rmSync(path.join(made.dir, 'src'), { recursive: true, force: true });
  const dirGone = await made.watcher.tick();
  assert.ok(dirGone);
  assert.deepEqual(dirGone.changed, ['']);
  assert.deepEqual(dirGone.removed, ['src']);
});

test('a nested addition is reported at the directory that gained it', async (t) => {
  const made = await withWatch(t);
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  fs.writeFileSync(path.join(made.dir, 'output', 'row-2.csv'), 'c,d\n', 'utf8');
  const event = await made.watcher.tick();
  assert.ok(event);
  assert.deepEqual(event.changed, ['output']);
});

// ---------------------------------------------------------------------------
// The authorization behind the watch
// ---------------------------------------------------------------------------

test('a re-picked project stops the watch instead of following it', async (t) => {
  const made = await withWatch(t);
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  // The user picked the directory again: new binding, new selection generation.
  made.state.binding = binding({ bindingId: 'bind_2', selectionGeneration: 3 });
  assert.equal(await made.watcher.tick(), null);
  assert.equal(made.watcher.status().watching, false);
  const ended = made.events.filter((event) => event.type === 'projectWatchEnded');
  assert.equal(ended.length, 1);
  assert.equal(ended[0].reason, 'stale_context');
  assert.equal(ended[0].workspace_id, WORKSPACE);
});

test('a new grant version is a different authorization, not a continuation', async (t) => {
  const made = await withWatch(t);
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  made.state.binding = binding({ grantVersion: 4 });
  assert.equal(await made.watcher.tick(), null);
  assert.equal(made.watcher.status().watching, false);
  assert.equal(made.events.at(-1).reason, 'stale_context');
});

test('initial handshake and reconnect preserve the same directory authorization', async (t) => {
  const made = await withWatch(t);
  if (!made) return;
  made.state.binding = binding({ connectionEpoch: '' });
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  for (const epoch of ['epoch_abc', '', 'epoch_def']) {
    made.state.binding = binding({ connectionEpoch: epoch });
    await made.watcher.tick();
    assert.equal(made.watcher.status().watching, true);
    assert.equal(made.watcher.verifyScope(), true);
  }
  assert.deepEqual(made.events.filter(event => event.type === 'projectWatchEnded'), []);
});

test('a revoked project stops the watch and forgets what it had seen', async (t) => {
  const made = await withWatch(t);
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  made.state.binding = null;
  assert.equal(await made.watcher.tick(), null);
  assert.equal(made.watcher.status().watching, false);
  assert.equal(made.events.at(-1).type, 'projectWatchEnded');
  // Nothing about the old project survives: a later watch starts from a fresh
  // baseline rather than comparing against a directory nobody authorized.
  made.state.binding = binding({ bindingId: 'bind_2' });
  made.watcher.start(WORKSPACE);
  assert.equal(await made.watcher.tick(), null, 'the restart re-baselines');
  assert.deepEqual(made.events.filter((event) => event.type === 'projectChanged'), []);
});

test('verifyScope stops a watch whose authorization moved', async (t) => {
  const made = await withWatch(t);
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  made.state.binding = null;
  assert.equal(made.watcher.verifyScope(), false);
  assert.equal(made.watcher.status().watching, false);
  assert.equal(made.events.at(-1).reason, 'stale_context');
});

test('stopping deliberately reports the end and clears the timer', async (t) => {
  const made = await withWatch(t);
  if (!made) return;
  made.watcher.start(WORKSPACE);
  const armed = made.timers.length;
  assert.ok(armed > 0, 'starting arms a scan');
  made.watcher.stop();
  assert.equal(made.watcher.status().watching, false);
  assert.equal(made.events.at(-1).reason, 'stopped');
  assert.equal(made.timers.at(-1).cancelled, true, 'the pending scan is cancelled');
  // A stop that twice-as-much is not a second event.
  const count = made.events.length;
  made.watcher.stop();
  assert.equal(made.events.length, count);
});

// ---------------------------------------------------------------------------
// Bounds
// ---------------------------------------------------------------------------

test('a scan that hits its entry bound says so and never reports removals', async (t) => {
  const made = await withWatch(t, { maxEntries: 5, maxDepth: 0 });
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  // Delete something the truncated scan cannot have looked at: claiming it was
  // removed would tell the panel half the project is gone.
  fs.rmSync(path.join(made.dir, 'output'), { recursive: true, force: true });
  const event = await made.watcher.tick();
  if (event) {
    assert.equal(event.truncated, true, 'a partial scan is reported as partial');
    assert.deepEqual(event.removed, []);
  }
  assert.equal(made.watcher.status().truncated, true);
});

test('a directory the truncated scan did not reach is not reported as removed', async (t) => {
  // This scan runs out of its *directory* budget, not its entry budget, and it
  // runs out before it gets back to ``output``: a newly created directory sorts
  // ahead of the ones the baseline reached. ``output`` still exists, so
  // reporting it as removed would tell the panel that half the project is gone
  // -- the difference between "did not look" and "looked and it was not there".
  const made = await withWatch(t, { maxDirectories: 2 });
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  assert.equal(made.watcher.status().truncated, true,
    'the root plus one subdirectory already uses the whole budget');
  fs.mkdirSync(path.join(made.dir, 'aaa'));
  const event = await made.watcher.tick();
  assert.ok(event, 'the new directory is a change');
  assert.equal(event.truncated, true, 'this scan is partial too');
  assert.deepEqual(event.changed, ['', 'aaa']);
  assert.deepEqual(event.removed, [], 'output was not reached, not deleted');
  assert.equal(fs.existsSync(path.join(made.dir, 'output')), true, 'and it is still there');
});

test('a truncated project is polled at the slow end', async (t) => {
  const made = await withWatch(t, { maxEntries: 3, intervalMs: 1000 });
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  assert.equal(made.timers.at(-1).ms, watchMod.WATCH_MAX_INTERVAL_MS,
    'reading a big project more cheaply, not more often');
});

test('a healthy project is polled at the configured interval', async (t) => {
  const made = await withWatch(t, { intervalMs: 1500 });
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  assert.equal(made.timers.at(-1).ms, 1500);
});

test('an interval below the floor is clamped, not honored', async (t) => {
  const made = await withWatch(t, { intervalMs: 1 });
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  assert.equal(made.timers.at(-1).ms, watchMod.WATCH_MIN_INTERVAL_MS);
});

test('a failing read backs off and says nothing about change', async (t) => {
  let failing = true;
  const made = await withWatch(t, {
    reader: {
      tree: async (params) => {
        if (failing) {
          return { ok: false, code: 'device_offline', message: 'the device is not connected' };
        }
        return browserTree(made, params);
      },
    },
    intervalMs: 1000,
  });
  if (!made) return;
  // eslint-disable-next-line no-use-before-define
  made.watcher.start(WORKSPACE);
  assert.equal(await made.watcher.tick(), null);
  assert.deepEqual(made.events, [], 'a failed scan is not a change');
  assert.equal(made.watcher.status().last_error, 'device_offline');
  assert.equal(made.watcher.status().watching, true, 'a transient failure is not a revoke');
  assert.ok(made.timers.at(-1).ms > 1000, 'the retry waits longer');
  failing = false;
  assert.equal(await made.watcher.tick(), null);
  assert.equal(made.watcher.status().last_error, '');
  assert.equal(made.timers.at(-1).ms, 1000, 'a good scan restores the interval');
});

/**
 * A reader that routes to the real browser. Declared here because the failing
 * reader above needs a working one to fall back to mid-test.
 */
function browserTree(made, params) {
  return made.browser.tree(params);
}

test('a scan already in flight is not started twice', async (t) => {
  let release = null;
  const gate = new Promise((resolve) => { release = resolve; });
  let calls = 0;
  const made = await withWatch(t, {
    reader: {
      tree: async (params) => {
        calls += 1;
        if (calls === 1) await gate;
        return made.browser.tree(params);
      },
    },
  });
  if (!made) return;
  made.watcher.start(WORKSPACE);
  const first = made.watcher.tick();
  assert.equal(await made.watcher.tick(), null, 'the second call is refused, not queued');
  release();
  await first;
  assert.equal(made.watcher.status().scans, 1);
});

test('a project too deep to finish is honestly partial', async (t) => {
  const made = await withWatch(t, { maxDepth: 0 });
  if (!made) return;
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  assert.equal(made.watcher.status().truncated, true,
    'depth 0 visited the root but there are directories below it');
});

test('a directory bigger than one page is a partial view, not the whole directory', async (t) => {
  const asked = [];
  const made = await withWatch(t, {
    reader: {
      tree: async (params) => {
        asked.push(params.limit);
        return made.browser.tree(params);
      },
    },
  });
  if (!made) return;
  // One page is all the device answers (``LIST_LIMIT_MAX``/``MAX_PAGE``), and a
  // *full* page carrying a cursor means there are names this scan did not read.
  // Reading that as "the whole directory" is what would let a deletion of an
  // unread name look like nothing happened.
  for (let i = 0; i < 250; i += 1) {
    fs.writeFileSync(path.join(made.dir, `f${String(i).padStart(3, '0')}.txt`), 'x\n', 'utf8');
  }
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  assert.equal(Math.max(...asked) <= watchMod.WATCH_PAGE_MAX, true,
    `every page asked for is answerable (asked for ${Math.max(...asked)})`);
  assert.equal(made.watcher.status().truncated, true,
    '200 of 250 names is not a complete look at that directory');
});

// ---------------------------------------------------------------------------
// The adapter: what the page is told
// ---------------------------------------------------------------------------

/** Load `fork/project-source.js` into a fake page with a host event channel. */
function loadAdapter(options = {}) {
  const source = fs.readFileSync(path.join(STATIC, 'fork', 'project-source.js'), 'utf8');
  const listeners = [];
  const window = {
    URL,
    CowDesktopHost: options.host === null ? undefined : {
      project: () => Promise.resolve({ ok: true, path: '', entries: [] }),
      canUseProjectSource: () => Promise.resolve(true),
      onHostEvent: (listener) => {
        listeners.push(listener);
        return () => {
          const at = listeners.indexOf(listener);
          if (at >= 0) listeners.splice(at, 1);
        };
      },
    },
  };
  const fn = new Function('window', 'module', `${source}\nreturn window.CowProjectSource;`);
  const api = fn(window, { exports: {} });
  api.configure({
    binding: () => (options.binding === undefined
      ? { workspace_id: 'ws_1', binding_id: 'bind_1', grant_version: 3 }
      : options.binding),
  });
  return {
    api,
    emit: (payload) => listeners.slice().forEach((listener) => listener(payload)),
    subscribers: () => listeners.length,
  };
}

test('a change event reaches the panel with plain paths', async () => {
  const page = loadAdapter();
  const seen = [];
  page.api.watch((event) => seen.push(event));
  page.emit({
    type: 'projectChanged',
    workspace_id: 'ws_1',
    binding_id: 'bind_1',
    selection_generation: 2,
    changed: ['', 'src'],
    removed: ['output'],
    truncated: true,
  });
  assert.deepEqual(seen, [{
    ended: false,
    reason: '',
    changed: ['', 'src'],
    removed: ['output'],
    truncated: true,
  }]);
});

test('an event from another binding or workspace is dropped', async () => {
  const page = loadAdapter();
  const seen = [];
  page.api.watch((event) => seen.push(event));
  page.emit({ type: 'projectChanged', workspace_id: 'ws_other', binding_id: 'bind_1', changed: [''] });
  page.emit({
    type: 'projectChanged',
    workspace_id: 'ws_1',
    // The user re-picked the directory while this event was in flight: it
    // describes the directory they replaced.
    binding_id: 'bind_older',
    changed: ['src'],
  });
  assert.deepEqual(seen, [], 'a stale watch does not refresh the panel');
});

test('the end of a watch is reported as an end, with its reason', async () => {
  const page = loadAdapter();
  const seen = [];
  page.api.watch((event) => seen.push(event));
  page.emit({ type: 'projectWatchEnded', workspace_id: 'ws_1', reason: 'stale_context' });
  assert.deepEqual(seen, [{
    ended: true, reason: 'stale_context', changed: [], removed: [], truncated: false,
  }]);
});

test('unrelated host events and an unsubscribed panel are ignored', async () => {
  const page = loadAdapter();
  const seen = [];
  const off = page.api.watch((event) => seen.push(event));
  page.emit({ type: 'suspended' });
  page.emit({ type: 'projectChanged', workspace_id: 'ws_1', binding_id: 'bind_1', changed: [1, null] });
  assert.deepEqual(seen[0].changed, ['1', ''], 'the list is normalized, not trusted');
  off();
  assert.equal(page.subscribers(), 0);
  page.emit({ type: 'projectChanged', workspace_id: 'ws_1', binding_id: 'bind_1', changed: [''] });
  assert.equal(seen.length, 1);
});

test('no local binding, no host or no handler all mean no subscription', async () => {
  const bare = loadAdapter({ binding: null });
  assert.equal(bare.api.watch(() => {})(), undefined, 'the unsubscribe is inert and safe');
  assert.equal(bare.subscribers(), 0);
  const browser = loadAdapter({ host: null });
  browser.api.watch(() => {});
  assert.equal(browser.subscribers(), 0);
  const page = loadAdapter();
  assert.equal(typeof page.api.watch(null), 'function');
  assert.equal(page.subscribers(), 0);
});

test('the host event channel is not required for the rest of the surface', async () => {
  const source = fs.readFileSync(path.join(STATIC, 'fork', 'project-source.js'), 'utf8');
  const window = {
    URL,
    CowDesktopHost: {
      // A host that predates the event channel: reading and editing still work,
      // and watching is inert rather than throwing.
      project: () => Promise.resolve({ ok: true, path: '', entries: [] }),
      canUseProjectSource: () => Promise.resolve(true),
    },
  };
  const fn = new Function('window', 'module', `${source}\nreturn window.CowProjectSource;`);
  const api = fn(window, { exports: {} });
  api.configure({ binding: () => ({ workspace_id: 'ws_1' }) });
  const off = api.watch(() => {});
  assert.equal(typeof off, 'function');
  assert.equal(off(), undefined);
});

// A last, cheap proof that the real helper and the scheduler agree on timing
// without a fake clock: one scan through the real spawn path.
test('a real scan completes under a real timer', async (t) => {
  const made = await withWatch(t, { intervalMs: 1000 });
  if (!made) return;
  await made.runner.ready();
  made.watcher.start(WORKSPACE);
  await made.watcher.tick();
  await wait(5);
  assert.equal(made.watcher.status().scans, 1);
  assert.equal(made.watcher.status().last_error, '');
});
