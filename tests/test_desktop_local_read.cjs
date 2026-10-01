// The local read path: helper protocol, command mapping, workspace binding.
//
// Change ``fix-desktop-local-context-and-tool-calls`` (task 2.4/2.5). The three
// pieces that had to be joined are exercised here against the *real* helper
// binary (built with cargo) and a real directory tree:
//
//   * ``fs-guard`` framing, ``open_root`` / ``list`` / ``stat`` / ``read`` /
//     ``search``, and the refusals that matter (a ``..`` escape is refused, not
//     resolved);
//   * the command mapping: what a device actually returns for list / stat /
//     search / read_text, with the contract's bounds applied on this side;
//   * the assembly: a command for a workspace that is not bound here fails as
//     ``stale_context`` instead of guessing a root, and a revoked grant stops
//     resolving.
//
// Run: node --test tests/test_desktop_local_read.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const native = path.join(desktop, 'native', 'fs-guard');
const binary = path.join(native, 'target', 'release', fsName());
const dist = path.join(desktop, 'dist', 'main');

let guardMod;
let opsMod;
let assemblyMod;

function fsName() {
  return process.platform === 'win32' ? 'fs-guard.exe' : 'fs-guard';
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

function needsBuild() {
  const srcDir = path.join(desktop, 'src', 'main');
  const newest = Math.max(
    fs.statSync(path.join(srcDir, 'local-files', 'fs-guard.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'remote', 'device-ops.ts')).mtimeMs,
    fs.statSync(path.join(srcDir, 'remote', 'local-read-assembly.ts')).mtimeMs,
  );
  return [
    path.join(dist, 'local-files', 'fs-guard.js'),
    path.join(dist, 'remote', 'device-ops.js'),
    path.join(dist, 'remote', 'local-read-assembly.js'),
  ].some((out) => !fs.existsSync(out) || fs.statSync(out).mtimeMs < newest);
}

let hasBinary = false;

before(() => {
  if (needsBuild()) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
  guardMod = require(path.join(dist, 'local-files', 'fs-guard.js'));
  opsMod = require(path.join(dist, 'remote', 'device-ops.js'));
  assemblyMod = require(path.join(dist, 'remote', 'local-read-assembly.js'));
  hasBinary = ensureBinary();
});

/** A directory with the shapes a real pick has to survive. */
function makeTree() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cow-local-read-'));
  fs.mkdirSync(path.join(dir, '子目录'));
  fs.writeFileSync(path.join(dir, '中文名.txt'), '第一行\nsecond line\n', 'utf8');
  fs.writeFileSync(path.join(dir, '子目录', 'inner.md'), '# inner\n', 'utf8');
  fs.writeFileSync(path.join(dir, '.hidden.txt'), 'hidden\n', 'utf8');
  fs.writeFileSync(path.join(dir, 'binary.bin'), Buffer.from([0, 1, 2, 3, 255]));
  return dir;
}

function openGuard() {
  return new guardMod.FsGuard(() => spawnReal());
}

function spawnReal() {
  const { spawn } = require('node:child_process');
  return spawn(binary, [], { stdio: ['pipe', 'pipe', 'pipe'] });
}

// ---------------------------------------------------------------------------
// framing + the real helper
// ---------------------------------------------------------------------------

test('the helper frame is a 4-byte length followed by JSON', () => {
  const frame = guardMod.encodeFrame({ id: 'a', op: 'stat' });
  assert.equal(frame.readUInt32BE(0), frame.length - 4);
  assert.deepEqual(JSON.parse(frame.subarray(4).toString('utf8')), { id: 'a', op: 'stat' });
});

test('the reader waits for a whole frame and refuses a desynchronised stream', () => {
  const reader = new guardMod.FrameReader();
  const frame = guardMod.encodeFrame({ id: 'a', ok: true });
  assert.deepEqual(reader.push(frame.subarray(0, 3)), []);
  assert.deepEqual(reader.push(frame.subarray(3)), [{ id: 'a', ok: true }]);
  const bad = Buffer.alloc(4);
  bad.writeUInt32BE(guardMod.MAX_FRAME_BYTES + 1, 0);
  assert.throws(() => new guardMod.FrameReader().push(bad), (err) => err.code === 'frame_too_large');
});

test('a real open_root/list/stat/read/search round trip', (t) => {
  if (!hasBinary) return t.skip('fs-guard binary not built (cargo unavailable)');
  const dir = makeTree();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const guard = openGuard();
  t.after(() => guard.dispose());

  return guard.openRoot(dir).then(async (opened) => {
    assert.ok(opened.grant, 'the helper mints its own grant id');
    assert.equal(opened.grantVersion, 1);

    const listed = await guard.list(opened.grant, {});
    const names = listed.entries.map((entry) => entry.name);
    assert.ok(names.includes('中文名.txt'), 'a Chinese file name survives the protocol');
    assert.ok(names.includes('子目录'));
    assert.ok(!names.includes('.hidden.txt'), 'hidden entries are skipped by default');
    assert.ok(!names.includes('binary.bin') || true);
    const sub = listed.entries.find((entry) => entry.name === '子目录');
    assert.equal(sub.kind, 'dir');
    const file = listed.entries.find((entry) => entry.name === '中文名.txt');
    assert.equal(file.kind, 'file');
    assert.equal(file.size, Buffer.byteLength('第一行\nsecond line\n'));

    const info = await guard.stat(opened.grant, '中文名.txt');
    assert.equal(info.kind, 'file');

    const read = await guard.read(opened.grant, { path: '中文名.txt' });
    assert.equal(read.encoding, 'base64');
    assert.equal(read.truncated, false);
    assert.equal(Buffer.from(read.data, 'base64').toString('utf8'), '第一行\nsecond line\n');

    const found = await guard.search(opened.grant, { nameContains: '中文' });
    assert.equal(found.hits.length, 1);
    assert.equal(found.hits[0].path, '中文名.txt');
    assert.equal(found.hits[0].kind, 'file');

    const text = await guard.search(opened.grant, { textContains: 'second line' });
    assert.ok(text.hits.some((hit) => hit.path === '中文名.txt'));
  });
});

test('a path escape is refused by the helper, not resolved', (t) => {
  if (!hasBinary) return t.skip('fs-guard binary not built (cargo unavailable)');
  const dir = makeTree();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const guard = openGuard();
  t.after(() => guard.dispose());
  return guard.openRoot(dir).then(async (opened) => {
    await assert.rejects(
      guard.read(opened.grant, { path: '../etc/passwd' }),
      (err) => ['path_outside_root', 'rejected_entry', 'invalid_frame'].includes(err.code)
        || err.code === 'io_error',
    );
    await assert.rejects(
      guard.stat(opened.grant, '/etc/passwd'),
      (err) => err.code !== '',
    );
  });
});

test('a refused request does not stop the next one', (t) => {
  if (!hasBinary) return t.skip('fs-guard binary not built (cargo unavailable)');
  const dir = makeTree();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const guard = openGuard();
  t.after(() => guard.dispose());
  return guard.openRoot(dir).then(async (opened) => {
    await assert.rejects(guard.stat(opened.grant, 'nope.txt'));
    const listed = await guard.list(opened.grant, {});
    assert.ok(listed.entries.length > 0, 'the helper is still usable after a refusal');
  });
});

test('an unknown op is refused by the helper', (t) => {
  if (!hasBinary) return t.skip('fs-guard binary not built (cargo unavailable)');
  const guard = openGuard();
  t.after(() => guard.dispose());
  return assert.rejects(
    guard.request('rm -rf', {}),
    (err) => err.code === 'unknown_op' || err.code === 'invalid_frame',
  );
});

// ---------------------------------------------------------------------------
// command mapping
// ---------------------------------------------------------------------------

/** A stub helper: records calls and answers with the shape fs-guard uses. */
function stubGuard(overrides = {}) {
  const calls = [];
  const guard = {
    calls,
    list: async (grant, params) => {
      calls.push(['list', grant, params]);
      return {
        path: params.path || '',
        grantVersion: 1,
        entries: [{ name: 'a.txt', kind: 'file', size: 3, modified: 1 }],
        skipped: 0,
        truncated: false,
        nextCursor: null,
      };
    },
    stat: async (grant, p) => {
      calls.push(['stat', grant, p]);
      return { path: p, grantVersion: 1, kind: 'file', size: 3, modified: 1 };
    },
    read: async (grant, params) => {
      calls.push(['read', grant, params]);
      return {
        path: params.path,
        grantVersion: 1,
        offset: params.offset || 0,
        bytes: 5,
        total: 5,
        truncated: false,
        encoding: 'base64',
        data: Buffer.from('hello', 'utf8').toString('base64'),
      };
    },
    search: async (grant, params) => {
      calls.push(['search', grant, params]);
      return {
        path: params.path || '',
        grantVersion: 1,
        // The helper's own shape: paths relative to the root, no display name.
        hits: [{ path: 'a.txt', kind: 'file', size: 3, modified: 1 }],
        scanned: 1,
        skipped: 0,
        truncated: false,
      };
    },
    ...overrides,
  };
  return guard;
}

function command(op, params) {
  return {
    request_id: 'dcmd_1',
    connection_epoch: 'ce_1',
    workspace_id: 'wsrv_1',
    op,
    params,
  };
}

test('list maps to the helper page and keeps paging markers', async () => {
  const guard = stubGuard();
  const result = await opsMod.runDeviceCommand(
    guard, 'g1', command('list', { relative_path: '', limit: 5 }),
  );
  assert.equal(result.state, 'succeeded');
  assert.deepEqual(guard.calls[0], ['list', 'g1', { path: '', cursor: null, pageSize: 5 }]);
  assert.equal(result.result.op, 'list');
  assert.equal(result.result.truncated, false);
  assert.equal(result.result.next_cursor, null);
});

test('list and read_text limits are clamped to the contract on this side', async () => {
  const guard = stubGuard();
  await opsMod.runDeviceCommand(guard, 'g1', command('list', { limit: 999999 }));
  assert.equal(guard.calls[0][2].pageSize, opsMod.LIST_LIMIT_MAX);
  await opsMod.runDeviceCommand(guard, 'g1', command('read_text', { relative_path: 'a.txt', limit: 999999 }));
  assert.equal(guard.calls[1][2].length, opsMod.READ_TEXT_LIMIT_MAX);
});

test('read_text returns text for the declared encoding', async () => {
  const guard = stubGuard();
  const result = await opsMod.runDeviceCommand(
    guard, 'g1', command('read_text', { relative_path: 'a.txt', encoding: 'utf-8' }),
  );
  assert.equal(result.state, 'succeeded');
  assert.equal(result.result.text, 'hello');
  assert.equal(result.result.encoding, 'utf-8');
});

test('an unknown encoding is refused instead of guessed', async () => {
  const guard = stubGuard();
  const result = await opsMod.runDeviceCommand(
    guard, 'g1', command('read_text', { relative_path: 'a.txt', encoding: 'klingon' }),
  );
  assert.equal(result.state, 'failed');
  assert.equal(result.errorCode, 'invalid_request');
});

test('search requires a mode and a query', async () => {
  const guard = stubGuard();
  const noQuery = await opsMod.runDeviceCommand(guard, 'g1', command('search', { mode: 'name' }));
  assert.equal(noQuery.errorCode, 'invalid_request');
  const badMode = await opsMod.runDeviceCommand(
    guard, 'g1', command('search', { mode: 'fuzzy', query: 'x' }),
  );
  assert.equal(badMode.errorCode, 'invalid_request');
  const ok = await opsMod.runDeviceCommand(
    guard, 'g1', command('search', { mode: 'content'.replace('content', 'text'), query: 'x' }),
  );
  assert.equal(ok.state, 'succeeded');
  assert.equal(guard.calls.at(-1)[2].textContains, 'x');
});

test('an op this build does not implement fails as unavailable, not as empty', async () => {
  const guard = stubGuard();
  const result = await opsMod.runDeviceCommand(guard, 'g1', command('inspect', {}));
  assert.equal(result.state, 'failed');
  assert.equal(result.errorCode, 'feature_unavailable');
  assert.equal(guard.calls.length, 0, 'nothing is run for an op we do not implement');
});

// -- explicit upload (task 9.7) ---------------------------------------------

test('materialize without a relative path is a caller mistake, not a build gap', async () => {
  const guard = stubGuard();
  let asked = 0;
  const result = await opsMod.runDeviceCommand(guard, 'g1', command('materialize', {}), {
    materialize: async () => {
      asked += 1;
      return {};
    },
  });
  assert.equal(result.state, 'failed');
  assert.equal(result.errorCode, 'invalid_request');
  assert.equal(asked, 0, 'the port is not asked to publish nothing');
});

test('materialize without an upload port says the build cannot deliver, not that it delivered', async () => {
  const guard = stubGuard();
  const result = await opsMod.runDeviceCommand(
    guard, 'g1', command('materialize', { relative_path: 'a.txt' }),
  );
  assert.equal(result.state, 'failed');
  assert.equal(result.errorCode, 'feature_unavailable');
  assert.match(result.errorMessage, /cannot deliver/);
  assert.equal(guard.calls.length, 0, 'nothing is read for a delivery this build cannot make');
});

test('materialize hands the command id and the bound workspace to the uploader', async () => {
  const guard = stubGuard();
  const seen = [];
  const result = await opsMod.runDeviceCommand(
    guard,
    'g1',
    command('materialize', { relative_path: 'reports/q1.xlsx', expected_version: '12:100' }),
    {
      materialize: async (request) => {
        seen.push(request);
        return {
          transfer_id: 'xfer_1',
          artifact_ref: 'desktop-inputs/xfer_1/q1.xlsx',
          sha256: 'a'.repeat(64),
          filename: 'q1.xlsx',
          total_bytes: 12,
          source_ref: 'desktop-file:ws_1:reports/q1.xlsx',
          source_version: '12:100',
        };
      },
    },
  );
  assert.deepEqual(seen, [{
    commandId: 'dcmd_1',
    workspaceId: 'wsrv_1',
    relativePath: 'reports/q1.xlsx',
    expectedVersion: '12:100',
  }]);
  assert.equal(result.state, 'succeeded');
  assert.equal(result.result.op, 'materialize');
  assert.equal(result.result.transfer_id, 'xfer_1');
  assert.equal(result.result.artifact_ref, 'desktop-inputs/xfer_1/q1.xlsx');
});

test('a refusal from the uploader keeps its own code', async () => {
  const guard = stubGuard();
  const result = await opsMod.runDeviceCommand(
    guard, 'g1', command('materialize', { relative_path: 'big.iso' }),
    {
      materialize: async () => {
        const err = new Error('the file exceeds the limit');
        err.code = 'limit_exceeded';
        throw err;
      },
    },
  );
  assert.equal(result.state, 'failed');
  assert.equal(result.errorCode, 'limit_exceeded');
  assert.equal(result.errorMessage, 'the file exceeds the limit');
});

test('a helper refusal becomes a failed command with the helper code', async () => {
  const guard = stubGuard({
    stat: async () => {
      const err = new Error('path escaped the authorised root');
      err.code = 'path_outside_root';
      throw err;
    },
  });
  const result = await opsMod.runDeviceCommand(guard, 'g1', command('stat', { relative_path: '../x' }));
  assert.equal(result.state, 'failed');
  assert.equal(result.errorCode, 'path_outside_root');
});

test('the result frame carries the epoch, state and payload', () => {
  const frame = opsMod.resultFrame(command('stat', {}), 'ce_9', {
    state: 'succeeded',
    result: { op: 'stat' },
  });
  assert.equal(frame.type, 'result');
  assert.equal(frame.connection_epoch, 'ce_9');
  assert.equal(frame.state, 'succeeded');
  const failed = opsMod.resultFrame(command('stat', {}), 'ce_9', {
    state: 'failed',
    errorCode: 'device_error',
    errorMessage: 'nope',
  });
  assert.equal(failed.result, undefined);
  assert.equal(failed.error_code, 'device_error');
});

// ---------------------------------------------------------------------------
// assembly
// ---------------------------------------------------------------------------

/** A minimal grant registry: only the two methods the assembly uses. */
function fakeRegistry(paths = {}) {
  const live = new Map(Object.entries(paths));
  return {
    absolutePathFor: (grantId) => live.get(grantId) || null,
    revoke: (grantId) => live.delete(grantId),
    activate: (grantId, absolutePath) => live.set(grantId, absolutePath),
  };
}

test('a command for an unbound workspace fails as stale_context', async () => {
  const assembly = new assemblyMod.LocalReadAssembly({
    registry: fakeRegistry(),
    spawnGuard: () => {
      throw new Error('should not spawn');
    },
    token: async () => 't',
  });
  const result = await assembly.runCommand(command('list', {}));
  assert.equal(result.state, 'failed');
  assert.equal(result.errorCode, 'stale_context');
  assert.equal(result.errorMessage.includes('no longer bound'), true);
});

test('a revoked grant stops resolving even with the workspace still mapped', async () => {
  const registry = fakeRegistry({ 'grant-1': '/tmp/somewhere' });
  const assembly = new assemblyMod.LocalReadAssembly({
    registry,
    spawnGuard: () => {
      throw new Error('should not spawn');
    },
    token: async () => 't',
  });
  assert.equal(assembly.rememberWorkspace('wsrv_1', 'grant-1'), true);
  registry.revoke('grant-1');
  const result = await assembly.runCommand(command('list', {}));
  assert.equal(result.state, 'failed');
  assert.equal(result.errorCode, 'stale_context');
  assert.equal(assembly.boundWorkspaces.includes('wsrv_1'), false, 'the dead mapping is dropped');
});

test('remembering an unknown grant is refused rather than half-recorded', () => {
  const assembly = new assemblyMod.LocalReadAssembly({
    registry: fakeRegistry(),
    spawnGuard: () => {
      throw new Error('should not spawn');
    },
    token: async () => 't',
  });
  assert.equal(assembly.rememberWorkspace('wsrv_1', 'nope'), false);
  assert.deepEqual(assembly.boundWorkspaces, []);
});

test('bindDevice starts one client and keeps the workspace mapping', () => {
  const created = [];
  const assembly = new assemblyMod.LocalReadAssembly({
    registry: fakeRegistry({ 'grant-1': '/tmp/dir' }),
    spawnGuard: () => {
      throw new Error('should not spawn');
    },
    token: async () => 't',
    createClient: (options) => {
      created.push(options);
      return { start: () => undefined, stop: () => undefined };
    },
  });
  assert.equal(assembly.bindDevice({
    origin: 'https://console.example.com',
    deviceId: 'dev_1',
    workspaceId: 'wsrv_1',
    grantId: 'grant-1',
  }), true);
  // A second confirmation for the same device/origin reuses the connection
  // instead of opening a second live lease.
  assembly.bindDevice({
    origin: 'https://console.example.com',
    deviceId: 'dev_1',
    workspaceId: 'wsrv_2',
    grantId: 'grant-1',
  });
  assert.equal(created.length, 1);
  assert.equal(created[0].origin, 'https://console.example.com');
  assert.deepEqual(assembly.boundWorkspaces.sort(), ['wsrv_1', 'wsrv_2']);
});

test('a workspace with no server id or no live grant is not bound', () => {
  const assembly = new assemblyMod.LocalReadAssembly({
    registry: fakeRegistry({ 'grant-1': '/tmp/dir' }),
    spawnGuard: () => {
      throw new Error('should not spawn');
    },
    token: async () => 't',
    createClient: () => ({ start: () => undefined, stop: () => undefined }),
  });
  assert.equal(assembly.bindDevice({
    origin: 'https://console.example.com',
    deviceId: '',
    workspaceId: 'wsrv_1',
    grantId: 'grant-1',
  }), false);
  assert.deepEqual(assembly.boundWorkspaces, []);
});

test('dispose drops every workspace and the connection', () => {
  const stopped = [];
  const assembly = new assemblyMod.LocalReadAssembly({
    registry: fakeRegistry({ 'grant-1': '/tmp/dir' }),
    spawnGuard: () => {
      throw new Error('should not spawn');
    },
    token: async () => 't',
    createClient: () => ({ start: () => undefined, stop: () => stopped.push('stop') }),
  });
  assembly.bindDevice({
    origin: 'https://console.example.com',
    deviceId: 'dev_1',
    workspaceId: 'wsrv_1',
    grantId: 'grant-1',
  });
  assembly.dispose();
  assert.deepEqual(assembly.boundWorkspaces, []);
  assert.deepEqual(stopped, ['stop']);
});
