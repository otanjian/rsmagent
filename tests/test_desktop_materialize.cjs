// The explicit hand-over of one local file to the server (change task 9.7).
//
// Two claims have to hold at the same time, and they are different claims:
//
//   * nothing here runs on its own -- a card, a preview, a refresh and a read
//     must never move bytes to the server, so the ops that *read* are exercised
//     with an upload transport installed and must leave it untouched;
//   * when the user does ask, the copy is made through the existing transfer
//     path, carries the provenance of the local file, and leaves the local file
//     exactly as it was.
//
// The helper under test is the *real* fs-guard binary and the source is a real
// file on disk, because the property that matters most -- "the bytes that were
// published are the bytes that were on disk, hashed as one file" -- is exactly
// the property a stub would fake.
//
// Run: node --test tests/test_desktop_materialize.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync, spawn } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const nativeDir = path.join(desktop, 'native', 'fs-guard');
const dist = path.join(desktop, 'dist', 'main');

const HELPERS = ['fs-guard.ts'];
const MODULES = [
  ['local-files/materialize.ts', 'local-files/materialize.js'],
  ['remote/materialize-transport.ts', 'remote/materialize-transport.js'],
  ['remote/device-ops.ts', 'remote/device-ops.js'],
];

let guardMod;
let materializeMod;
let transportMod;
let opsMod;
let hasBinary = false;

function fsName() {
  return process.platform === 'win32' ? 'fs-guard.exe' : 'fs-guard';
}

const binary = path.join(nativeDir, 'target', 'release', fsName());

/** The whole source surface this file depends on, so a stale build is rebuilt. */
function sourceFiles() {
  const files = HELPERS.map((name) => path.join(desktop, 'src', 'main', 'local-files', name));
  return files.concat(MODULES.map(([src]) => path.join(desktop, 'src', 'main', src)));
}

function needsBuild() {
  const newest = Math.max(...sourceFiles().map((file) => fs.statSync(file).mtimeMs));
  return MODULES.some(([, out]) => {
    const built = path.join(dist, out);
    return !fs.existsSync(built) || fs.statSync(built).mtimeMs < newest;
  });
}

function ensureBinary() {
  if (process.env.COW_SKIP_RUST_BUILD) return fs.existsSync(binary);
  let newest = 0;
  for (const name of fs.readdirSync(path.join(nativeDir, 'src'))) {
    newest = Math.max(newest, fs.statSync(path.join(nativeDir, 'src', name)).mtimeMs);
  }
  if (fs.existsSync(binary) && fs.statSync(binary).mtimeMs >= newest) return true;
  try {
    execFileSync('cargo', ['build', '--release', '--offline'], { cwd: nativeDir, stdio: 'pipe' });
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
  materializeMod = require(path.join(dist, 'local-files', 'materialize.js'));
  transportMod = require(path.join(dist, 'remote', 'materialize-transport.js'));
  opsMod = require(path.join(dist, 'remote', 'device-ops.js'));
  hasBinary = ensureBinary();
});

// ---------------------------------------------------------------------------
// harness
// ---------------------------------------------------------------------------

/** A real directory, a real guard process, a recording transfer transport. */
async function withFile(t, contents, run) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cow-materialize-'));
  const name = 'report.bin';
  fs.writeFileSync(path.join(dir, name), contents);
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  const guard = new guardMod.FsGuard(() => spawn(binary, [], { stdio: ['pipe', 'pipe', 'pipe'] }));
  t.after(() => guard.dispose());
  const opened = await guard.openRoot(dir);
  return run({ dir, name, guard, fsGrant: opened.grant });
}

function recordingTransport(overrides = {}) {
  const calls = { create: [], chunks: [], commit: [] };
  return {
    calls,
    transport: {
      create: async (args) => {
        calls.create.push(args);
        return { id: 'xfer_1', chunk_size: 8 * 1024, acknowledged_offset: 0 };
      },
      putChunk: async (transferId, offset, body, sha256) => {
        calls.chunks.push({ transferId, offset, body, sha256 });
        return { acknowledged_offset: offset + body.length, state: 'receiving' };
      },
      commit: async (args) => {
        calls.commit.push(args);
        return { state: 'committed', artifact_ref: 'desktop-inputs/xfer_1/report.bin' };
      },
      ...overrides,
    },
  };
}

// ---------------------------------------------------------------------------
// names and versions
// ---------------------------------------------------------------------------

test('the provenance token names the project and the file, never a directory', () => {
  assert.equal(
    materializeMod.sourceRefFor('ws_1', 'reports/q1.xlsx'),
    'desktop-file:ws_1:reports/q1.xlsx',
  );
  const ref = materializeMod.sourceRefFor('ws_1', '/Users/someone/secret.txt');
  assert.ok(!ref.includes('/Users'), 'an absolute path cannot leak through the source ref');
  assert.equal(materializeMod.sourceRefFor('', 'a.txt'), 'desktop-file:unbound:a.txt');
});

test('the published name is the last segment with control characters removed', () => {
  assert.equal(materializeMod.safeLocalName('reports/q1.xlsx'), 'q1.xlsx');
  assert.equal(materializeMod.safeLocalName('a\\b\\c.txt'), 'c.txt');
  assert.equal(materializeMod.safeLocalName('\u0000'), 'file');
  assert.equal(materializeMod.safeLocalName('..'), 'file');
});

test('the version is the same string a stat result carries', () => {
  assert.equal(materializeMod.localVersionOf(12, 100.9), '12:100');
});

// ---------------------------------------------------------------------------
// the upload itself, over the real helper
// ---------------------------------------------------------------------------

test('a file larger than one helper read is published whole, hashed as one file', async (t) => {
  if (!hasBinary) return t.skip('fs-guard binary not built (cargo unavailable)');
  // 100 KiB: more than three helper reads (32 KiB each), so the window is
  // assembled from several bounded reads rather than one lucky request.
  const body = Buffer.alloc(100 * 1024);
  for (let i = 0; i < body.length; i += 1) body[i] = i % 251;
  await withFile(t, body, async ({ guard, fsGrant, dir }) => {
    const { transport, calls } = recordingTransport();
    const outcome = await materializeMod.materializeLocalFile({
      guard,
      fsGrant,
      relativePath: 'report.bin',
      commandId: 'cmd_1',
      workspaceId: 'ws_1',
      transport,
      chunkBytes: 40 * 1024,
    });

    assert.equal(calls.create.length, 1);
    assert.equal(calls.create[0].commandId, 'cmd_1');
    assert.equal(calls.create[0].sourceRef, 'desktop-file:ws_1:report.bin');
    assert.equal(calls.create[0].totalBytes, body.length);

    // Every window arrives with its own digest, and the windows tile the file.
    const assembled = Buffer.concat(calls.chunks.map((chunk) => chunk.body));
    assert.equal(assembled.length, body.length);
    assert.ok(assembled.equals(body), 'the published bytes are the bytes on disk');
    for (const chunk of calls.chunks) {
      assert.equal(chunk.transferId, 'xfer_1');
      assert.equal(
        chunk.sha256,
        crypto.createHash('sha256').update(chunk.body).digest('hex'),
      );
    }
    assert.deepEqual(
      calls.chunks.map((chunk) => chunk.offset),
      [0, 40 * 1024, 80 * 1024],
      'windows are sequential and contiguous',
    );

    // The commit declares the whole-file digest and the version it opened with.
    const digest = crypto.createHash('sha256').update(body).digest('hex');
    assert.equal(calls.commit[0].sha256, digest);
    assert.equal(calls.commit[0].totalBytes, body.length);
    assert.equal(calls.commit[0].sourceVersionAfter, outcome.source_version);
    assert.equal(calls.commit[0].transferId, 'xfer_1');

    assert.equal(outcome.transfer_id, 'xfer_1');
    assert.equal(outcome.artifact_ref, 'desktop-inputs/xfer_1/report.bin');
    assert.equal(outcome.sha256, digest);
    assert.equal(outcome.filename, 'report.bin');
    assert.equal(outcome.total_bytes, body.length);

    // The local file is untouched: same bytes, same directory contents.
    assert.ok(fs.readFileSync(path.join(dir, 'report.bin')).equals(body));
    assert.deepEqual(fs.readdirSync(dir), ['report.bin']);
  });
});

test('the published copy keeps a version that matches what stat reported', async (t) => {
  if (!hasBinary) return t.skip('fs-guard binary not built (cargo unavailable)');
  await withFile(t, Buffer.from('quarterly numbers\n'), async ({ guard, fsGrant }) => {
    const info = await guard.stat(fsGrant, 'report.bin');
    const version = materializeMod.localVersionOf(info.size, info.modified);
    const { transport: upload } = recordingTransport();
    const outcome = await materializeMod.materializeLocalFile({
      guard,
      fsGrant,
      relativePath: 'report.bin',
      expectedVersion: version,
      commandId: 'cmd_2',
      workspaceId: 'ws_1',
      transport: upload,
    });
    assert.equal(outcome.source_version, version);
  });
});

// ---------------------------------------------------------------------------
// refusals
// ---------------------------------------------------------------------------

test('a file that is not the one approved is refused before any transfer opens', async (t) => {
  if (!hasBinary) return t.skip('fs-guard binary not built (cargo unavailable)');
  await withFile(t, Buffer.from('one'), async ({ guard, fsGrant }) => {
    const { transport, calls } = recordingTransport();
    await assert.rejects(
      () => materializeMod.materializeLocalFile({
        guard,
        fsGrant,
        relativePath: 'report.bin',
        expectedVersion: '3:1',
        commandId: 'cmd_3',
        workspaceId: 'ws_1',
        transport,
      }),
      (err) => err.code === 'file_changed',
    );
    assert.equal(calls.create.length, 0, 'nothing is reserved for a stale approval');
  });
});

test('a directory is refused, and a path escape never reaches the transfer', async (t) => {
  if (!hasBinary) return t.skip('fs-guard binary not built (cargo unavailable)');
  await withFile(t, Buffer.from('x'), async ({ guard, fsGrant, dir }) => {
    fs.mkdirSync(path.join(dir, 'nested'));
    const { transport, calls } = recordingTransport();
    await assert.rejects(
      () => materializeMod.materializeLocalFile({
        guard, fsGrant, relativePath: 'nested', commandId: 'cmd_4',
        workspaceId: 'ws_1', transport,
      }),
      (err) => err.code === 'not_a_file',
    );
    await assert.rejects(
      () => materializeMod.materializeLocalFile({
        guard, fsGrant, relativePath: '../report.bin', commandId: 'cmd_5',
        workspaceId: 'ws_1', transport,
      }),
      (err) => err.code !== '',
    );
    await assert.rejects(
      () => materializeMod.materializeLocalFile({
        guard, fsGrant, relativePath: '', commandId: 'cmd_6',
        workspaceId: 'ws_1', transport,
      }),
      (err) => err.code === 'invalid_request',
    );
    assert.equal(calls.create.length, 0, 'the transfer layer is never asked about a refused path');
  });
});

test('a file past the server bound is refused here, before anything moves', async (t) => {
  if (!hasBinary) return t.skip('fs-guard binary not built (cargo unavailable)');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cow-materialize-big-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  // A sparse file: the size the decision reads, without the disk it would need.
  fs.writeFileSync(path.join(dir, 'huge.bin'), '');
  fs.truncateSync(path.join(dir, 'huge.bin'), materializeMod.MATERIALIZE_MAX_BYTES + 1);

  const guard = new guardMod.FsGuard(() => spawn(binary, [], { stdio: ['pipe', 'pipe', 'pipe'] }));
  t.after(() => guard.dispose());
  const opened = await guard.openRoot(dir);
  const { transport, calls } = recordingTransport();
  await assert.rejects(
    () => materializeMod.materializeLocalFile({
      guard, fsGrant: opened.grant, relativePath: 'huge.bin', commandId: 'cmd_7',
      workspaceId: 'ws_1', transport,
    }),
    (err) => err.code === 'limit_exceeded',
  );
  assert.equal(calls.create.length, 0);
});

test('a commit with no artifact reference is a failure, not a silent success', async (t) => {
  if (!hasBinary) return t.skip('fs-guard binary not built (cargo unavailable)');
  await withFile(t, Buffer.from('payload'), async ({ guard, fsGrant }) => {
    const { transport } = recordingTransport({
      commit: async () => ({ state: 'committed' }),
    });
    await assert.rejects(
      () => materializeMod.materializeLocalFile({
        guard, fsGrant, relativePath: 'report.bin', commandId: 'cmd_8',
        workspaceId: 'ws_1', transport,
      }),
      (err) => err.code === 'publish_failed',
    );
  });
});

// ---------------------------------------------------------------------------
// nothing uploads unless asked
// ---------------------------------------------------------------------------

test('no read op touches the upload transport', async (t) => {
  if (!hasBinary) return t.skip('fs-guard binary not built (cargo unavailable)');
  await withFile(t, Buffer.from('content\n'), async ({ guard, fsGrant }) => {
    const { transport, calls } = recordingTransport();
    let published = 0;
    const port = async (request) => {
      published += 1;
      return materializeMod.materializeLocalFile({
        guard,
        fsGrant,
        commandId: request.commandId,
        workspaceId: request.workspaceId,
        relativePath: request.relativePath,
        expectedVersion: request.expectedVersion,
        transport,
      });
    };
    const command = (op, params) => ({
      request_id: 'dcmd_1',
      connection_epoch: 'ce_1',
      workspace_id: 'wsrv_1',
      op,
      params,
    });

    for (const [op, params] of [
      ['list', { relative_path: '' }],
      ['stat', { relative_path: 'report.bin' }],
      ['search', { relative_path: '', query: 'report', mode: 'name' }],
      ['read_text', { relative_path: 'report.bin' }],
    ]) {
      const result = await opsMod.runDeviceCommand(guard, fsGrant, command(op, params), {
        materialize: port,
      });
      assert.equal(result.state, 'succeeded', `${op} still works with an upload port installed`);
    }

    assert.equal(published, 0, 'reading a file is not handing it over');
    assert.equal(calls.create.length, 0);
    assert.equal(calls.chunks.length, 0);
    assert.equal(calls.commit.length, 0);
  });
});

// ---------------------------------------------------------------------------
// the HTTP transport
// ---------------------------------------------------------------------------

function fakeFetch(responses) {
  const seen = [];
  const calls = [...responses];
  const impl = async (url, init) => {
    seen.push({ url, init });
    const next = calls.shift();
    if (!next) throw new Error('no scripted response left');
    if (next instanceof Error) throw next;
    return {
      ok: next.status >= 200 && next.status < 300,
      status: next.status,
      json: async () => next.body,
    };
  };
  return { impl, seen };
}

test('the transport posts the contract bodies and keeps the bearer off the body', async () => {
  const { impl, seen } = fakeFetch([
    { status: 201, body: { status: 'success', data: { id: 'xfer_9', chunk_size: 4096, acknowledged_offset: 0 } } },
    { status: 200, body: { status: 'success', data: { acknowledged_offset: 3, state: 'receiving' } } },
    { status: 200, body: { status: 'success', data: { state: 'committed', artifact_ref: 'desktop-inputs/xfer_9/a.bin' } } },
  ]);
  const transport = transportMod.createMaterializeTransport({
    tenantId: () => 'tenant_1',
    origin: 'https://cow.example.com',
    token: async () => 'ntok',
    fetch: impl,
  });

  const created = await transport.create({
    commandId: 'cmd_9',
    sourceRef: 'desktop-file:ws_1:a.bin',
    sourceVersion: '3:1',
    totalBytes: 3,
    filename: 'a.bin',
  });
  assert.equal(created.id, 'xfer_9');
  assert.equal(created.chunk_size, 4096);
  assert.equal(seen[0].url, 'https://cow.example.com/api/desktop/transfers');
  assert.equal(seen[0].init.method, 'POST');
  assert.equal(seen[0].init.headers.Authorization, 'Bearer ntok');
  assert.equal(seen[0].init.headers['X-Tenant-ID'], 'tenant_1');
  const createBody = JSON.parse(seen[0].init.body);
  assert.deepEqual(createBody, {
    command_id: 'cmd_9',
    source_ref: 'desktop-file:ws_1:a.bin',
    source_version: '3:1',
    total_bytes: 3,
    filename: 'a.bin',
  });

  await transport.putChunk('xfer_9', 0, Buffer.from('abc'), 'digest');
  assert.equal(seen[1].url, 'https://cow.example.com/api/desktop/transfers/xfer_9/chunks/0');
  assert.equal(seen[1].init.method, 'PUT');
  assert.equal(seen[1].init.headers['X-Content-SHA256'], 'digest');
  assert.equal(seen[1].init.headers['X-Tenant-ID'], 'tenant_1');
  assert.equal(seen[1].init.headers['Content-Type'], undefined, 'a chunk is not JSON');

  const committed = await transport.commit({
    transferId: 'xfer_9',
    totalBytes: 3,
    sha256: 'digest',
    sourceVersionAfter: '3:1',
  });
  assert.equal(committed.artifact_ref, 'desktop-inputs/xfer_9/a.bin');
  assert.equal(seen[2].init.headers['X-Tenant-ID'], 'tenant_1');
  assert.equal(
    seen[2].url,
    'https://cow.example.com/api/desktop/transfers/xfer_9/commit',
  );
});

test('a server refusal keeps the server code instead of a generic failure', async () => {
  const { impl } = fakeFetch([
    { status: 409, body: { status: 'error', message: 'source file changed during transfer', code: 'file_changed' } },
  ]);
  const transport = transportMod.createMaterializeTransport({
    tenantId: () => 'tenant_1',
    origin: 'https://cow.example.com',
    token: async () => 'ntok',
    fetch: impl,
  });
  await assert.rejects(
    () => transport.create({
      commandId: 'c', sourceRef: 's', sourceVersion: '1:1', totalBytes: 1, filename: 'a',
    }),
    (err) => err.code === 'file_changed' && /changed/.test(err.message),
  );
});

test('transfer re-reads the broker tenant and refuses a missing selection before HTTP', async () => {
  let tenant = 'tenant_old';
  const { impl, seen } = fakeFetch([
    { status: 201, body: { data: { id: 'xfer_1' } } },
    { status: 201, body: { data: { id: 'xfer_2' } } },
  ]);
  const transport = transportMod.createMaterializeTransport({
    origin: 'https://cow.example.com', token: async () => 'ntok',
    tenantId: () => tenant, fetch: impl,
  });
  const args = { commandId: 'c', sourceRef: 's', sourceVersion: '1:1', totalBytes: 1, filename: 'a' };
  await transport.create(args);
  tenant = 'tenant_new';
  await transport.create(args);
  assert.deepEqual(seen.map(call => call.init.headers['X-Tenant-ID']), ['tenant_old', 'tenant_new']);
  tenant = null;
  await assert.rejects(transport.create(args), err => err.code === 'missing_tenant');
  assert.equal(seen.length, 2);
});

test('a transport failure is distinguishable from a refusal, and hides the URL', async () => {
  const { impl } = fakeFetch([new Error('connect ECONNREFUSED https://cow.example.com/api/desktop/transfers')]);
  const transport = transportMod.createMaterializeTransport({
    tenantId: () => 'tenant_1',
    origin: 'https://cow.example.com',
    token: async () => 'ntok',
    fetch: impl,
  });
  await assert.rejects(
    () => transport.create({
      commandId: 'c', sourceRef: 's', sourceVersion: '1:1', totalBytes: 1, filename: 'a',
    }),
    (err) => err.code === 'transport_error' && !/https?:/.test(err.message),
  );
});

test('an origin this build cannot trust is refused before a token is read', async () => {
  for (const [origin, message] of [
    ['http://cow.example.com', /https/],
    ['https://user:pw@cow.example.com', /userinfo/],
    ['https://cow.example.com/path', /path/],
    ['https://cow.example.com?x=1', /query/],
    ['not-a-url', /URL/],
  ]) {
    let read = 0;
    assert.throws(
      () => transportMod.createMaterializeTransport({
        tenantId: () => 'tenant_1',
        origin,
        token: async () => {
          read += 1;
          return 'ntok';
        },
        fetch: async () => {
          throw new Error('must not be called');
        },
      }),
      message,
      `origin ${origin} must be refused`,
    );
    assert.equal(read, 0, 'a refused origin never asks for a session');
  }
  // The bundled loopback backend is the one http origin allowed, and only when
  // the registered origin is exactly it.
  assert.equal(
    transportMod.materializeBaseUrl('http://127.0.0.1:8899', 'http://127.0.0.1:8899'),
    'http://127.0.0.1:8899',
  );
  assert.throws(
    () => transportMod.materializeBaseUrl('http://127.0.0.1:8899', 'http://127.0.0.1:9000'),
    /https/,
  );
});

test('a device with no session refuses to send rather than sending unauthenticated', async () => {
  const { impl, seen } = fakeFetch([]);
  const transport = transportMod.createMaterializeTransport({
    tenantId: () => 'tenant_1',
    origin: 'https://cow.example.com',
    token: async () => null,
    fetch: impl,
  });
  await assert.rejects(
    () => transport.create({
      commandId: 'c', sourceRef: 's', sourceVersion: '1:1', totalBytes: 1, filename: 'a',
    }),
    (err) => err.code === 'auth_required',
  );
  assert.equal(seen.length, 0);
});

test('the window and the ceiling are the contract\'s numbers, not this file\'s opinion', () => {
  const contract = JSON.parse(
    fs.readFileSync(path.join(root, 'contracts/desktop/v1.json'), 'utf8'));
  // Two ends, one number each: the server refuses a bigger chunk and a bigger
  // file, so a device that invented its own bound would discover it at commit.
  assert.equal(materializeMod.MATERIALIZE_CHUNK_BYTES,
    contract.limits.transfer_chunk_max_bytes);
  assert.equal(materializeMod.MATERIALIZE_MAX_BYTES, contract.limits.file_max_bytes);
  // And the helper's per-read bound is the helper's own (native/fs-guard).
  assert.ok(materializeMod.GUARD_READ_CHUNK <= 32 * 1024,
    'the guard read bound must not exceed the helper\'s');
});

test('a file larger than the helper\'s per-request ceiling still travels in one window', async (t) => {
  if (!hasBinary) return t.skip('fs-guard binary not built (cargo unavailable)');
  // 1.2 MiB: past the helper's 1 MiB per-request ceiling while still inside one
  // transfer window (4 MiB). A window that asked the guard for all 4 MiB would
  // be refused by the helper, so this pins "ask in the helper's bound, assemble
  // the window here" rather than "ask for whatever we want".
  const body = Buffer.alloc(1200 * 1024);
  for (let i = 0; i < body.length; i += 1) body[i] = (i * 7) % 256;
  await withFile(t, body, async ({ guard, fsGrant }) => {
    const { transport, calls } = recordingTransport();
    const outcome = await materializeMod.materializeLocalFile({
      guard,
      fsGrant,
      relativePath: 'report.bin',
      commandId: 'cmd_big',
      workspaceId: 'ws_1',
      transport,
    });
    assert.equal(outcome.total_bytes, body.length);
    assert.equal(calls.chunks.length, 1, 'one window carries the default chunk size');
    assert.ok(calls.chunks[0].body.equals(body), 'the window is the whole file');
    assert.equal(
      calls.commit[0].sha256,
      crypto.createHash('sha256').update(body).digest('hex'),
    );
  });
});
