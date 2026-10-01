// The *production seam* of the execution journal, driven through the assembly.
//
// Change ``align-desktop-project-execution-with-master``, tasks 7.1 / 7.6.
//
// Why this file exists, separately from ``test_desktop_execution_journal.cjs``
// and ``test_desktop_device_execution.cjs``: those two prove the journal and the
// runner *behave*, by constructing them directly. Neither proves the app ever
// *calls* them. That distinction is not academic -- the retention sweep was
// fully implemented, fully tested, and reachable from nothing, so a real install
// would have grown ``execution-journal.json`` without bound while every suite
// stayed green. A unit test cannot catch a missing caller; only a test that goes
// in the way the app goes in can.
//
// So this file drives ``LocalReadAssembly``, the object the main process owns,
// and asserts the housekeeping it is responsible for actually happens on the
// first v2 frame: an unfinished intent is recovered, and receipts older than the
// contract's retention window are reclaimed.
//
// Run: node --test tests/test_desktop_execution_wiring.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const srcMain = path.join(desktop, 'src', 'main');
const distMain = path.join(desktop, 'dist', 'main');
const assemblyPath = path.join(distMain, 'remote', 'local-read-assembly.js');
const journalPath = path.join(distMain, 'project-execution', 'journal.js');
const contractPath = path.join(distMain, 'project-execution', 'generated-contract.js');

let assemblyMod;
let journalMod;
let contract;

/** Rebuild when any main-process source is newer than the compiled assembly. */
function needsBuild() {
  if (!fs.existsSync(assemblyPath) || !fs.existsSync(journalPath)) return true;
  const compiledAt = fs.statSync(assemblyPath).mtimeMs;
  const newest = (dir) => Math.max(
    ...fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) return [newest(full)];
      return entry.name.endsWith('.ts') ? [fs.statSync(full).mtimeMs] : [];
    }),
  );
  return newest(srcMain) > compiledAt;
}

before(() => {
  if (needsBuild()) {
    execFileSync('npm', ['run', 'build:main'], { cwd: desktop, stdio: 'inherit' });
  }
  assemblyMod = require(assemblyPath);
  journalMod = require(journalPath);
  contract = require(contractPath);
});

/** A private user-data directory per test: the journal is per installation. */
function scratch() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'execution-wiring-'));
}

function record(overrides = {}) {
  return {
    journal_id: 'jrn-1',
    command_id: 'cmd-1',
    params_digest: 'sha256:aaaa',
    origin: 'https://master.example',
    user_id: 'u-1',
    tenant_id: 't-1',
    device_id: 'dev-1',
    workspace_id: 'ws-1',
    run_id: 'run-1',
    tool_call_id: 'call-1',
    tool: 'write_file',
    grant_version: 3,
    started_at: 1_700_000_000_000,
    ...overrides,
  };
}

function scopeOf(rec) {
  return {
    origin: rec.origin,
    user_id: rec.user_id,
    tenant_id: rec.tenant_id,
    device_id: rec.device_id,
    command_id: rec.command_id,
  };
}

/**
 * The assembly the main process actually builds.
 *
 * Only the execution channel matters here, and the guard is never spawned: the
 * seam under test runs before any tool is reached, which is the point -- the
 * recovery answer has to exist before the first frame is admitted.
 */
function assemblyFor(journalFile, onReconcile) {
  return new assemblyMod.LocalReadAssembly({
    registry: { get: () => null, revoke: () => undefined },
    spawnGuard: () => {
      throw new Error('the wiring seam must not spawn a helper');
    },
    token: async () => 't',
    ...(onReconcile ? { onReconcile } : {}),
    execution: {
      journalFile,
      backendPath: desktop,
      runtime: null,
      probe: () => null,
    },
  });
}

/**
 * One post-reconnect status query.
 *
 * This is an ordinary production call -- the server asks what became of a
 * command after the socket came back -- and it binds no grant, so it is the
 * cheapest honest way to reach the seam without a live connection.
 */
const statusQuery = { type: 'execution_status', command_id: 'cmd-probe', workspace_id: 'ws-1' };

test('7.6: reaching the execution endpoint recovers an unfinished intent', async () => {
  const dir = scratch();
  const file = path.join(dir, 'execution-journal.json');

  // A start intent that never got a receipt: the process died in between.
  const before_ = new journalMod.JournalStore({ file });
  before_.begin(record({ journal_id: 'jrn-lost', command_id: 'cmd-lost' }),
    scopeOf(record({ command_id: 'cmd-lost' })));

  const reconciled = [];
  const assembly = assemblyFor(file, (entry, reason) => reconciled.push({ entry, reason }));
  await assembly.runExecutionStatus(statusQuery);

  assert.equal(reconciled.length, 1, 'the unfinished intent is surfaced once');
  assert.equal(reconciled[0].entry.command_id, 'cmd-lost');

  // And it is *unknown*, never "not run" -- the distinction the whole journal
  // exists to keep.
  const after = new journalMod.JournalStore({ file });
  assert.equal(
    after.lookup('cmd-lost', 'sha256:aaaa', scopeOf(record({ command_id: 'cmd-lost' }))).kind,
    'unknown',
  );
});

test('7.6: reaching the execution endpoint reclaims receipts past the retention window', async () => {
  const dir = scratch();
  const file = path.join(dir, 'execution-journal.json');

  // A receipt that finished well outside the contract's retention window. The
  // clock is injected only while *writing* the fixture: the production sweep is
  // then judged against the real clock, which is exactly the comparison that
  // decides whether a real install's journal grows forever.
  const retainDays = contract.EXECUTION_LIMITS.journal_retain_days;
  const longAgo = Date.now() - (retainDays + 30) * 24 * 60 * 60 * 1000;
  const aged = record({ journal_id: 'jrn-aged', command_id: 'cmd-aged' });
  const seed = new journalMod.JournalStore({ file, now: () => longAgo });
  seed.begin(aged, scopeOf(aged));
  seed.complete('cmd-aged', aged.params_digest, {
    outcome: 'succeeded',
    effects: 'applied',
  }, scopeOf(aged));

  const assembly = assemblyFor(file);
  await assembly.runExecutionStatus(statusQuery);

  const after = new journalMod.JournalStore({ file });
  assert.deepEqual(
    after.list().map((entry) => entry.command_id),
    [],
    'the aged receipt is reclaimed',
  );
  // Reclaimed, not forgotten: the dedup key survives as a tombstone, so the
  // spent command cannot come back looking fresh.
  assert.equal(
    after.lookup('cmd-aged', aged.params_digest, scopeOf(aged)).kind,
    'pruned',
  );
});
