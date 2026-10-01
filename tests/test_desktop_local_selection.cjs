// Native directory-selection service: generation / cancel / ready-state
// (change `align-desktop-project-execution-with-master`, tasks 2.3 and 2.4).
//
// The service is pure Node with the native parts injected, so the properties that
// matter -- a stale dialog callback cannot activate a root the user has moved past,
// cancelling keeps the previous authorization, every failure reports what is still
// in effect -- are exercised for real. `pick` stands in for the OS dialog, which is
// the one thing a test cannot own; everything else is the production code path.
//
// Run: node --test tests/test_desktop_local_selection.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const compiledDir = path.join(desktop, 'dist', 'main', 'local-files');

let selection;
let grants;
let bridge;

function needsBuild() {
  const srcDir = path.join(desktop, 'src', 'main', 'local-files');
  if (!fs.existsSync(srcDir)) return true;
  const sources = fs.readdirSync(srcDir)
    .filter((name) => name.endsWith('.ts'))
    .map((name) => path.join(srcDir, name));
  if (!sources.length) return true;
  const newestSource = Math.max(...sources.map((p) => fs.statSync(p).mtimeMs));
  for (const out of ['selection.js', 'grants.js']) {
    const file = path.join(compiledDir, out);
    if (!fs.existsSync(file)) return true;
    if (fs.statSync(file).mtimeMs < newestSource) return true;
  }
  return false;
}

before(() => {
  if (needsBuild()) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
  selection = require(path.join(compiledDir, 'selection.js'));
  grants = require(path.join(compiledDir, 'grants.js'));
  bridge = require(path.join(desktop, 'dist', 'main', 'remote', 'local-files-bridge.js'));
});

const SCOPE = { serverId: 'srv_1', userId: 'usr_1', tenantId: 'tnt_1', deviceId: 'dev_1' };
const OTHER_SCOPE = { ...SCOPE, tenantId: 'tnt_2' };

function tempDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

/**
 * A service whose picker answers from a programed queue.
 *
 * `request(scope, extra)` builds one attempt; `extra` overrides `prepare`,
 * `release` or the picker for that attempt alone. `calls` records what the
 * service asked of its collaborators.
 */
function makeService(outcomes) {
  const calls = { pick: [], commit: [], release: [], described: [] };
  let index = 0;
  const service = new selection.DirectorySelectionService({
    messageFor: (purpose) => {
      calls.described.push(purpose);
      return purpose === 'project-execution'
        ? 'open this folder as my project'
        : 'let the server read files here';
    },
  });
  const request = (scope, extra = {}) => ({
    scope,
    purpose: 'readonly-input',
    pick: async (req) => {
      calls.pick.push(req);
      const next = outcomes[index++];
      if (next === undefined) throw new Error('the test programed too few picker outcomes');
      if (typeof next === 'function') return next(req);
      return next;
    },
    commit: ({ absolutePath, label, purpose }) => {
      calls.commit.push({ absolutePath, label, purpose });
      return {
        id: `grant_${calls.commit.length}`,
        label,
        grantVersion: calls.commit.length,
        purpose,
        serverId: scope.serverId,
        userId: scope.userId,
        tenantId: scope.tenantId,
        deviceId: scope.deviceId,
      };
    },
    ...extra,
  });
  return { service, calls, request };
}

/** A deferred the test resolves by hand, standing in for a slow dialog. */
function deferred() {
  let settle;
  const promise = new Promise((resolve) => {
    settle = resolve;
  });
  return { promise, settle };
}

/** Attaches a release hook that records released grant ids. */
function withRelease(calls) {
  return (grant) => {
    calls.release.push(grant.id);
  };
}

// ---------------------------------------------------------------------------
// 2.4 ready-state machine, happy path

test('a selection walks idle -> selecting -> ready and commits exactly once', async () => {
  const dir = tempDir('sel-happy-');
  const { service, calls, request } = makeService([{ filePaths: [dir] }]);
  const promise = service.select(request(SCOPE));
  // Readable while the picker is open: that is the point of publishing a phase.
  const during = service.state(SCOPE).phase;
  const outcome = await promise;

  assert.equal(during, 'selecting');
  assert.equal(outcome.state.phase, 'ready');
  assert.equal(outcome.ok, true);
  assert.equal(outcome.committed, true);
  assert.equal(calls.commit.length, 1);
  assert.equal(calls.commit[0].absolutePath, dir);
  assert.equal(calls.commit[0].label, path.basename(dir));
  assert.equal(calls.commit[0].purpose, 'readonly-input');
  assert.equal(outcome.state.generation, 1);
  assert.equal(outcome.state.effective.label, path.basename(dir));
  assert.equal(outcome.state.error, null);
});

test('the recorded state never carries the absolute path', async () => {
  const dir = tempDir('sel-nopath-');
  const { service, request } = makeService([{ filePaths: [dir] }]);
  const outcome = await service.select(request(SCOPE));
  const serialised = JSON.stringify(outcome.state);
  assert.equal(serialised.includes(dir), false, `state leaked the picked path: ${serialised}`);
  assert.equal(serialised.includes('absolutePath'), false);
});

test('the picker is told which authorization the user is about to make', async () => {
  const dir = tempDir('sel-purpose-');
  const { service, calls, request } = makeService([{ filePaths: [dir] }]);
  const outcome = await service.select(request(SCOPE, { purpose: 'project-execution' }));
  assert.equal(calls.pick.length, 1);
  assert.equal(calls.pick[0].purpose, 'project-execution');
  assert.equal(calls.pick[0].message, 'open this folder as my project');
  assert.equal(calls.commit[0].purpose, 'project-execution');
  assert.equal(outcome.committed, true);
});

test('a read-only selection is described as read-only', async () => {
  const dir = tempDir('sel-readonly-');
  const { service, calls, request } = makeService([{ filePaths: [dir] }]);
  await service.select(request(SCOPE));
  assert.deepEqual(calls.described, ['readonly-input']);
  assert.equal(calls.pick[0].message, 'let the server read files here');
});

// ---------------------------------------------------------------------------
// 2.4 cancel semantics

test('cancelling reports cancelled and leaves the previous selection in effect', async () => {
  const first = tempDir('sel-keep-a-');
  const { service, calls, request } = makeService([{ filePaths: [first] }, { canceled: true }]);
  const ready = await service.select(request(SCOPE));
  assert.equal(ready.committed, true);

  const cancelled = await service.select(request(SCOPE));
  assert.equal(cancelled.ok, true);
  assert.equal(cancelled.committed, false);
  assert.equal(cancelled.reason, 'cancelled');
  // Still the first directory: cancelling a second pick does not withdraw the
  // authorization the user already made.
  assert.equal(cancelled.state.effective.id, ready.state.effective.id);
  assert.equal(cancelled.state.phase, 'ready');
  assert.equal(cancelled.state.error, null);
  assert.equal(calls.commit.length, 1);
});

test('cancelling the very first attempt leaves nothing in effect', async () => {
  const { service, request } = makeService([{ canceled: true }]);
  const outcome = await service.select(request(SCOPE));
  assert.equal(outcome.committed, false);
  assert.equal(outcome.reason, 'cancelled');
  assert.equal(outcome.state.effective, null);
  assert.equal(outcome.state.phase, 'idle');
});

test('an empty path list or a null result counts as a cancel', async () => {
  const { service, request } = makeService([{ filePaths: [] }, null]);
  for (const _ of [0, 1]) {
    const outcome = await service.select(request(SCOPE));
    assert.equal(outcome.committed, false);
    assert.equal(outcome.reason, 'cancelled');
  }
});

test('every attempt bumps the generation, including cancelled ones', async () => {
  const dir = tempDir('sel-gen-');
  const { service, request } = makeService([{ canceled: true }, { filePaths: [dir] }]);
  const first = await service.select(request(SCOPE));
  const second = await service.select(request(SCOPE));
  assert.equal(first.state.generation, 1);
  assert.equal(second.state.generation, 2);
  assert.equal(service.state(SCOPE).generation, 2);
});

// ---------------------------------------------------------------------------
// 2.4 generation guard: a stale callback must not commit

test('an older attempt resolving after a newer one started is discarded', async () => {
  const older = tempDir('sel-old-');
  const newer = tempDir('sel-new-');
  const slowDialog = deferred();
  const { service, calls, request } = makeService([
    () => slowDialog.promise,
    { filePaths: [newer] },
  ]);

  const firstAttempt = service.select(request(SCOPE));
  // The user gives up on the first dialog and re-picks; the second resolves first.
  const secondOutcome = await service.select(request(SCOPE));
  assert.equal(secondOutcome.committed, true);
  assert.equal(secondOutcome.state.effective.label, path.basename(newer));

  // The abandoned first dialog finally answers. Its Promise callback still runs --
  // that is the hazard -- and it must not activate `older`.
  slowDialog.settle({ filePaths: [older] });
  const firstOutcome = await firstAttempt;
  assert.equal(firstOutcome.ok, true);
  assert.equal(firstOutcome.committed, false);
  assert.equal(firstOutcome.reason, 'superseded');
  assert.equal(calls.commit.length, 1, 'the superseded attempt committed a root');
  assert.equal(calls.commit[0].absolutePath, newer);
  assert.equal(service.state(SCOPE).effective.label, path.basename(newer));
});

test('a superseded attempt does not overwrite the newer attempt state', async () => {
  const newer = tempDir('sel-new2-');
  const slowDialog = deferred();
  const { service, request } = makeService([() => slowDialog.promise, { filePaths: [newer] }]);
  const firstAttempt = service.select(request(SCOPE));
  const secondOutcome = await service.select(request(SCOPE));
  slowDialog.settle({ filePaths: [tempDir('sel-old2-')] });
  await firstAttempt;
  // The stale attempt must not reset the phase or clear the effective selection of
  // the state that now belongs to the newer attempt.
  assert.equal(service.state(SCOPE).phase, 'ready');
  assert.equal(service.state(SCOPE).effective.id, secondOutcome.state.effective.id);
});

test('a superseded attempt does not run validation or commit', async () => {
  const newer = tempDir('sel-new3-');
  const slowDialog = deferred();
  const validated = [];
  const service = new selection.DirectorySelectionService({
    messageFor: () => 'pick',
    validate: async (absolutePath) => {
      validated.push(absolutePath);
      return { ok: true };
    },
  });
  const picker = (result) => async () => result;
  const commit = () => ({ id: 'grant_x' });
  const first = service.select({
    scope: SCOPE,
    purpose: 'readonly-input',
    pick: () => slowDialog.promise,
    commit,
  });
  const second = await service.select({
    scope: SCOPE,
    purpose: 'readonly-input',
    pick: picker({ filePaths: [newer] }),
    commit,
  });
  assert.equal(second.committed, true);

  slowDialog.settle({ filePaths: [tempDir('sel-old3-')] });
  const firstOutcome = await first;
  assert.equal(firstOutcome.reason, 'superseded');
  assert.deepEqual(validated, [newer], 'the superseded attempt still validated a path');
});

// ---------------------------------------------------------------------------
// 2.3 / 2.4 failure semantics

test('a non-directory selection fails and keeps the previous selection', async () => {
  const dir = tempDir('sel-failkeep-');
  const file = path.join(dir, 'not-a-dir.txt');
  fs.writeFileSync(file, 'x');
  const { service, calls, request } = makeService([{ filePaths: [dir] }, { filePaths: [file] }]);
  const ready = await service.select(request(SCOPE));
  const failed = await service.select(request(SCOPE));
  assert.equal(failed.ok, false);
  assert.equal(failed.code, 'not_a_directory');
  assert.equal(failed.state.phase, 'failed');
  assert.equal(failed.state.error.code, 'not_a_directory');
  // The first directory is still the authorization in effect.
  assert.equal(failed.state.effective.id, ready.state.effective.id);
  assert.equal(calls.commit.length, 1);
});

test('a missing selection fails as unreadable rather than activating', async () => {
  const { service, calls, request } = makeService([{ filePaths: ['/definitely/not/here/xyz'] }]);
  const outcome = await service.select(request(SCOPE));
  assert.equal(outcome.ok, false);
  assert.equal(outcome.code, 'not_a_directory');
  assert.equal(outcome.state.effective, null);
  assert.equal(calls.commit.length, 0);
});

test('validation is a real filesystem check, not the picker word', async () => {
  const file = path.join(os.tmpdir(), `sel-real-${process.pid}.txt`);
  fs.writeFileSync(file, 'x');
  const notDir = await selection.validateDirectory(file);
  assert.equal(notDir.ok, false);
  assert.equal(notDir.code, 'not_a_directory');

  const missing = await selection.validateDirectory(path.join(os.tmpdir(), 'nope-does-not-exist-xyz'));
  assert.equal(missing.ok, false);
  assert.equal(missing.code, 'not_a_directory');

  assert.deepEqual(await selection.validateDirectory(tempDir('sel-real-')), { ok: true });
});

test('a picker that throws is reported as a failure, with the previous selection kept', async () => {
  const dir = tempDir('sel-throw-');
  const { service, request } = makeService([
    { filePaths: [dir] },
    () => {
      throw new Error('dialog exploded');
    },
  ]);
  const ready = await service.select(request(SCOPE));
  const failed = await service.select(request(SCOPE));
  assert.equal(failed.ok, false);
  assert.equal(failed.code, 'picker_failed');
  assert.equal(failed.state.effective.id, ready.state.effective.id);
});

test('a commit that refuses (invalid scope) fails without inventing a selection', async () => {
  const dir = tempDir('sel-refuse-');
  const service = new selection.DirectorySelectionService({ messageFor: () => 'pick' });
  const outcome = await service.select({
    scope: SCOPE,
    purpose: 'readonly-input',
    pick: async () => ({ filePaths: [dir] }),
    commit: () => {
      const err = new Error('chooseWorkspace requires a full scope');
      err.code = 'invalid_request';
      throw err;
    },
  });
  assert.equal(outcome.ok, false);
  assert.equal(outcome.code, 'invalid_request');
  assert.equal(outcome.state.effective, null);
});

// ---------------------------------------------------------------------------
// 2.4 preparing phase

test('with a prepare step the phase passes through preparing', async () => {
  const dir = tempDir('sel-prep-');
  const seen = [];
  const { service, request } = makeService([{ filePaths: [dir] }]);
  const outcome = await service.select(
    request(SCOPE, {
      prepare: async (grant) => {
        seen.push(service.state(SCOPE).phase);
        assert.equal(grant.label, path.basename(dir));
        return { ok: true };
      },
    }),
  );
  assert.deepEqual(seen, ['preparing']);
  assert.equal(outcome.committed, true);
  assert.equal(outcome.state.phase, 'ready');
});

test('a failed prepare is reported and the discarded grant is released', async () => {
  const dir = tempDir('sel-prepfail-');
  const { service, calls, request } = makeService([{ filePaths: [dir] }]);
  const outcome = await service.select(
    request(SCOPE, {
      release: withRelease(calls),
      prepare: async () => ({ ok: false, code: 'worker_unavailable', message: 'worker would not start' }),
    }),
  );
  assert.equal(outcome.ok, false);
  assert.equal(outcome.code, 'worker_unavailable');
  assert.equal(outcome.state.phase, 'failed');
  // Released, so nothing is in effect -- and the state says so.
  assert.equal(calls.release.length, 1);
  assert.equal(outcome.state.effective, null);
});

test('a failed prepare without a release hook reports the grant as still in effect', async () => {
  const dir = tempDir('sel-prepnorel-');
  const { service, request } = makeService([{ filePaths: [dir] }]);
  const outcome = await service.select(
    request(SCOPE, { prepare: async () => ({ ok: false, code: 'worker_unavailable' }) }),
  );
  assert.equal(outcome.ok, false);
  // The service cannot un-activate it, so it does not claim the previous selection
  // is back while the registry still holds this one.
  assert.equal(outcome.state.effective.id, 'grant_1');
});

test('a throwing prepare is reported as prepare_failed and released', async () => {
  const dir = tempDir('sel-prepthrow-');
  const { service, calls, request } = makeService([{ filePaths: [dir] }]);
  const outcome = await service.select(
    request(SCOPE, {
      release: withRelease(calls),
      prepare: async () => {
        throw new Error('spawn ENOENT');
      },
    }),
  );
  assert.equal(outcome.ok, false);
  assert.equal(outcome.code, 'prepare_failed');
  assert.equal(outcome.message.includes('ENOENT'), true);
  assert.equal(calls.release.length, 1);
});

test('a new attempt during preparation supersedes it and releases its grant', async () => {
  const first = tempDir('sel-preprace-a-');
  const second = tempDir('sel-preprace-b-');
  const slowPrepare = deferred();
  let preparingStarted;
  const preparing = new Promise((resolve) => {
    preparingStarted = resolve;
  });
  const { service, calls, request } = makeService([
    { filePaths: [first] },
    { filePaths: [second] },
  ]);
  const slow = service.select(
    request(SCOPE, {
      release: withRelease(calls),
      prepare: () => {
        preparingStarted();
        return slowPrepare.promise.then(() => ({ ok: true }));
      },
    }),
  );
  // Wait until the first attempt has committed and is inside `prepare`, so the
  // newer attempt genuinely overtakes it *after* the grant exists. (A newer attempt
  // started earlier would simply win the pre-commit generation check, which the
  // "does not run validation or commit" case already covers.)
  await preparing;
  assert.equal(calls.commit.length, 1);

  const fast = await service.select(request(SCOPE, { prepare: async () => ({ ok: true }) }));
  assert.equal(fast.committed, true);

  slowPrepare.settle();
  const slowOutcome = await slow;
  assert.equal(slowOutcome.reason, 'superseded');
  assert.equal(calls.release.length, 1, 'the abandoned preparation did not release its grant');
  assert.equal(service.state(SCOPE).effective.label, path.basename(second));
});

// ---------------------------------------------------------------------------
// scope isolation and revocation

test('state is per scope and never crosses tenants or devices', async () => {
  const a = tempDir('sel-scope-a-');
  const b = tempDir('sel-scope-b-');
  const { service, request } = makeService([{ filePaths: [a] }, { filePaths: [b] }]);
  await service.select(request(SCOPE));
  assert.equal(service.state(OTHER_SCOPE).phase, 'idle');
  assert.equal(service.state(OTHER_SCOPE).effective, null);

  await service.select(request(OTHER_SCOPE));
  assert.equal(service.state(SCOPE).effective.label, path.basename(a));
  assert.equal(service.state(OTHER_SCOPE).effective.label, path.basename(b));
});

test('markRevoked clears the effective selection but keeps the generation', async () => {
  const dir = tempDir('sel-revoke-');
  const { service, request } = makeService([{ filePaths: [dir] }]);
  await service.select(request(SCOPE));
  const before = service.state(SCOPE);

  service.markRevoked(SCOPE);
  const after = service.state(SCOPE);
  assert.equal(after.effective, null);
  assert.equal(after.phase, 'idle');
  // The generation is kept so an attempt in flight from before the revoke is still
  // recognised as stale rather than committing over the revocation.
  assert.equal(after.generation, before.generation);
});

test('markRevoked does not resurrect a failed state as ready', async () => {
  const { service } = makeService([]);
  service.markRevoked(SCOPE);
  assert.equal(service.state(SCOPE).phase, 'idle');
});

test('forget drops only the named scopes', async () => {
  const a = tempDir('sel-forget-a-');
  const b = tempDir('sel-forget-b-');
  const { service, request } = makeService([{ filePaths: [a] }, { filePaths: [b] }]);
  await service.select(request(SCOPE));
  await service.select(request(OTHER_SCOPE));

  service.forget({ tenantId: 'tnt_1' });
  assert.equal(service.state(SCOPE).effective, null);
  assert.equal(service.state(SCOPE).generation, 0);
  assert.equal(service.state(OTHER_SCOPE).effective.label, path.basename(b));

  // An empty partial forgets everything, which is the logout / device reset path.
  service.forget({});
  assert.equal(service.state(OTHER_SCOPE).effective, null);
});

test('markCommitted records a grant the caller activated elsewhere', async () => {
  const { service } = makeService([]);
  service.markCommitted(SCOPE, {
    id: 'g9',
    label: 'restored',
    grantVersion: 1,
    purpose: 'project-execution',
    ...SCOPE,
  });
  assert.equal(service.state(SCOPE).phase, 'ready');
  assert.equal(service.state(SCOPE).effective.id, 'g9');
});

test('state() returns a copy, so a caller cannot mutate the service', async () => {
  const dir = tempDir('sel-copy-');
  const { service, request } = makeService([{ filePaths: [dir] }]);
  await service.select(request(SCOPE));
  const snapshot = service.state(SCOPE);
  snapshot.phase = 'failed';
  snapshot.effective = null;
  assert.equal(service.state(SCOPE).phase, 'ready');
  assert.equal(service.state(SCOPE).effective.label, path.basename(dir));
});

// ---------------------------------------------------------------------------
// the seam the dispatcher actually uses

test('the real bridge primitive activates the root the service committed', async () => {
  // Not a stand-in for the commit path: the real `activateGrant` against the real
  // process-wide registry, driven by the real selection service.
  bridge.remoteGrantRegistry.clear();
  const dir = tempDir('sel-wire-');
  const candidates = { version: 1, candidates: [] };
  const service = new selection.DirectorySelectionService({ messageFor: () => 'pick' });
  const outcome = await service.select({
    scope: SCOPE,
    purpose: 'readonly-input',
    pick: async () => ({ filePaths: [dir] }),
    commit: ({ absolutePath, purpose }) => {
      const activated = bridge.activateGrant({ scope: SCOPE, absolutePath, purpose, candidates });
      assert.equal(activated.ok, true);
      return activated.grant;
    },
    release: (grant) => {
      bridge.remoteGrantRegistry.revoke(grant.id);
    },
  });
  assert.equal(outcome.committed, true);
  assert.equal(candidates.candidates.length, 1);
  assert.equal(candidates.candidates[0].label, path.basename(dir));

  const live = bridge.remoteGrantRegistry.get(SCOPE);
  assert.equal(live.id, outcome.state.effective.id);
  // The registry still holds the path; the state that goes to the page does not.
  assert.equal(bridge.remoteGrantRegistry.absolutePathFor(live.id), dir);
  assert.equal(JSON.stringify(outcome.state).includes(dir), false);
});

test('the real bridge primitive cannot be widened past the asked-for purpose', async () => {
  bridge.remoteGrantRegistry.clear();
  const dir = tempDir('sel-wire-purpose-');
  const candidates = { version: 1, candidates: [] };
  const service = new selection.DirectorySelectionService({ messageFor: () => 'pick' });
  const outcome = await service.select({
    scope: SCOPE,
    purpose: 'readonly-input',
    pick: async () => ({ filePaths: [dir] }),
    commit: ({ absolutePath, purpose }) =>
      bridge.activateGrant({ scope: SCOPE, absolutePath, purpose, candidates }).grant,
  });
  assert.equal(outcome.committed, true);
  const live = bridge.remoteGrantRegistry.get(SCOPE);
  assert.equal(live.purpose, 'readonly-input');
  assert.equal(grants.allowsProjectExecution(live), false);
});
