// Desktop local-files single-chunk transfer window (change task 10.4).
//
// Run: node --test tests/test_desktop_transfer_frontend.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const compiledDir = path.join(desktop, 'dist', 'main', 'local-files');

let transfer;

function needsBuild() {
  const src = path.join(desktop, 'src', 'main', 'local-files', 'transfer.ts');
  const out = path.join(compiledDir, 'transfer.js');
  if (!fs.existsSync(out)) return true;
  return fs.statSync(out).mtimeMs < fs.statSync(src).mtimeMs;
}

before(() => {
  if (needsBuild()) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], {
      cwd: desktop,
      stdio: 'pipe',
    });
  }
  transfer = require(path.join(compiledDir, 'transfer.js'));
});

function tmpFile(contents) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'xfer-'));
  const file = path.join(dir, 'sample.bin');
  fs.writeFileSync(file, contents);
  return { dir, file };
}

test('sourceVersionOf fingerprints size and mtime', () => {
  assert.equal(transfer.sourceVersionOf(12, 100.7), '12:100');
});

test('uploadWithSingleChunkWindow posts sequential chunks and commits', async () => {
  const body = Buffer.from('abcdefghijklmnopqrstuvwxyz');
  const { file, dir } = tmpFile(body);
  const handle = transfer.openSourceHandle(file, 'src:sample.bin');
  const chunks = [];
  const result = await transfer.uploadWithSingleChunkWindow({
    handle,
    filename: 'sample.bin',
    commandId: 'cmd_1',
    chunkSize: 8,
    create: async ({ commandId, sourceRef, sourceVersion, totalBytes }) => {
      assert.equal(commandId, 'cmd_1', 'a transfer names the command it belongs to');
      assert.equal(sourceRef, 'src:sample.bin');
      assert.equal(totalBytes, body.length);
      assert.ok(sourceVersion.includes(':'));
      return { id: 'xfer_1', chunk_size: 8, acknowledged_offset: 0 };
    },
    putChunk: async (transferId, offset, buf, sha256) => {
      assert.equal(transferId, 'xfer_1', 'every chunk names its own transfer');
      chunks.push({ offset, len: buf.length, sha256 });
      assert.equal(
        sha256,
        crypto.createHash('sha256').update(buf).digest('hex'),
      );
      return { acknowledged_offset: offset + buf.length, state: 'receiving' };
    },
    commit: async ({ transferId, totalBytes, sha256 }) => {
      assert.equal(transferId, 'xfer_1');
      assert.equal(totalBytes, body.length);
      assert.equal(
        sha256,
        crypto.createHash('sha256').update(body).digest('hex'),
      );
      return { state: 'committed', artifact_ref: 'desktop-inputs/xfer_1/sample.bin' };
    },
  });
  await handle.close();
  assert.equal(result.transferId, 'xfer_1');
  assert.equal(chunks.length, Math.ceil(body.length / 8));
  assert.equal(chunks[0].offset, 0);
  assert.equal(chunks[1].offset, 8);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('upload retries once on file_changed then succeeds', async () => {
  const body = Buffer.from('version-one-payload!!');
  const { file, dir } = tmpFile(body);
  const handle = transfer.openSourceHandle(file, 'src:v.bin');
  let creates = 0;
  let forceChange = true;
  const result = await transfer.uploadWithSingleChunkWindow({
    handle,
    filename: 'v.bin',
    commandId: 'cmd_v',
    chunkSize: 64,
    create: async () => {
      creates += 1;
      return { id: 'xfer_' + creates, chunk_size: 64, acknowledged_offset: 0 };
    },
    putChunk: async (_transferId, offset, buf) => {
      if (forceChange) {
        forceChange = false;
        // Mutate the on-disk file so restat after the loop sees a new version.
        fs.writeFileSync(file, Buffer.from('version-two-payload!!'));
        throw new transfer.TransferError('file_changed', 'simulated');
      }
      return { acknowledged_offset: offset + buf.length, state: 'receiving' };
    },
    commit: async ({ transferId }) => ({
      state: 'committed',
      artifact_ref: 'desktop-inputs/' + transferId + '/v.bin',
    }),
  });
  await handle.close();
  assert.equal(creates, 2, 'exactly one automatic retry');
  assert.equal(result.transferId, 'xfer_2');
  fs.rmSync(dir, { recursive: true, force: true });
});

test('upload gives up after a second file_changed', async () => {
  const body = Buffer.from('unstable');
  const { file, dir } = tmpFile(body);
  const handle = transfer.openSourceHandle(file, 'src:u.bin');
  await assert.rejects(
    () =>
      transfer.uploadWithSingleChunkWindow({
        handle,
        filename: 'u.bin',
        commandId: 'cmd_u',
        create: async () => ({
          id: 'xfer_x',
          chunk_size: 64,
          acknowledged_offset: 0,
        }),
        putChunk: async () => {
          throw new transfer.TransferError('file_changed', 'always');
        },
        commit: async () => ({ state: 'committed' }),
      }),
    (err) => err && err.code === 'file_changed',
  );
  await handle.close();
  fs.rmSync(dir, { recursive: true, force: true });
});
