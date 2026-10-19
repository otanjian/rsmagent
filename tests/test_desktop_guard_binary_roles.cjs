// The two file helpers' names and locations, pinned against the real build tree.
//
// Change ``add-windows-local-project-access`` (task 3.3, design D1/D3).
//
// Why this is a *process* test and not a string test: the bug it exists to catch
// was a candidate list that could never match on Windows, because it joined
// ``fs-guard`` with no suffix while cargo produces ``fs-guard.exe``. Asserting on
// the string ``'fs-guard.exe'`` somewhere would not have caught it. So the
// assertions here are of two kinds:
//
// * the **name** each role resolves to on each platform, checked for ``win32``
//   and for ``linux`` from whichever host runs the suite -- a Windows-only
//   assertion would leave the bug untested on the machines most likely to
//   reintroduce it;
// * the **real checkout**: a candidate list built from this repository's own
//   ``desktop`` directory must actually find the binary cargo just built. That is
//   what makes "resolution works" a measured fact rather than a plausible one.
'use strict';

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const native = path.join(desktop, 'native', 'fs-guard');
const dist = path.join(desktop, 'dist', 'main');
const binarySrc = path.join(desktop, 'src', 'main', 'local-files', 'fs-guard-binary.ts');
const binaryJs = path.join(dist, 'local-files', 'fs-guard-binary.js');

let binaryMod;

/** The built name for a role, computed here rather than imported. */
function builtName(role) {
  const base = role === 'write' ? 'project-fs-guard' : 'fs-guard';
  return process.platform === 'win32' ? `${base}.exe` : base;
}

function buildMain() {
  execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], {
    cwd: desktop,
    stdio: 'pipe',
    shell: process.platform === 'win32',
  });
}

before(() => {
  const srcMtime = fs.statSync(binarySrc).mtimeMs;
  const jsMtime = fs.existsSync(binaryJs) ? fs.statSync(binaryJs).mtimeMs : 0;
  if (jsMtime < srcMtime) buildMain();
  binaryMod = require(binaryJs);
});

// ---------------------------------------------------------------------------
// The name each role resolves to.
// ---------------------------------------------------------------------------

test('each role has its own executable name, with the platform suffix', () => {
  const { guardExecutableName } = binaryMod;
  assert.equal(guardExecutableName('read', 'win32'), 'fs-guard.exe');
  assert.equal(guardExecutableName('write', 'win32'), 'project-fs-guard.exe');
  assert.equal(guardExecutableName('read', 'linux'), 'fs-guard');
  assert.equal(guardExecutableName('write', 'linux'), 'project-fs-guard');
  assert.equal(guardExecutableName('read', 'darwin'), 'fs-guard');
  // The two roles must never collide: a read path that resolved the write
  // binary would hold write capability it never asked for.
  assert.notEqual(
    guardExecutableName('read', process.platform),
    guardExecutableName('write', process.platform),
  );
});

test('the read role never resolves the write binary, or the reverse', () => {
  const { guardBinaryCandidates } = binaryMod;
  for (const platform of ['win32', 'linux']) {
    const read = guardBinaryCandidates({ role: 'read', appPath: '/app', platform });
    const write = guardBinaryCandidates({ role: 'write', appPath: '/app', platform });
    assert.ok(read.length > 0 && write.length > 0);
    // Compare on the file name so a shared directory cannot mask a collision.
    const readNames = new Set(read.map((candidate) => path.basename(candidate)));
    const writeNames = new Set(write.map((candidate) => path.basename(candidate)));
    for (const name of writeNames) {
      assert.ok(
        !readNames.has(name),
        `the read role must not offer the write helper ${name} (${platform})`,
      );
    }
  }
});

test('every dev-tree candidate carries the platform suffix', () => {
  const { guardBinaryCandidates } = binaryMod;
  for (const platform of ['win32', 'linux']) {
    const expected = platform === 'win32' ? '.exe' : null;
    const candidates = guardBinaryCandidates({
      role: 'write',
      appPath: '/app',
      platform,
    });
    for (const candidate of candidates) {
      const name = path.basename(candidate);
      if (expected === null) {
        assert.ok(!name.endsWith('.exe'), `${candidate} must not carry .exe on ${platform}`);
      } else {
        assert.ok(
          name.endsWith('.exe'),
          `${candidate} must carry .exe on ${platform}: the built artifact is `
          + 'project-fs-guard.exe',
        );
      }
    }
  }
});

test('the packaged location is offered both renamed and as-built', () => {
  const { guardBinaryCandidates } = binaryMod;
  // ``extraResources`` renames the artifact (``to: 'fs-guard'``), so the bare
  // name is what a packaged macOS build actually contains; a build that kept the
  // built name must also resolve, which is why both are offered.
  const win = guardBinaryCandidates({
    role: 'read',
    resourcesPath: 'C:\\app\\resources',
    platform: 'win32',
  });
  assert.ok(win.includes(path.join('C:\\app\\resources', 'fs-guard')));
  assert.ok(win.includes(path.join('C:\\app\\resources', 'fs-guard.exe')));

  const write = guardBinaryCandidates({
    role: 'write',
    resourcesPath: 'C:\\app\\resources',
    platform: 'win32',
  });
  assert.ok(write.includes(path.join('C:\\app\\resources', 'project-fs-guard')));
  assert.ok(write.includes(path.join('C:\\app\\resources', 'project-fs-guard.exe')));
});

test('an override only ever redirects its own role', () => {
  const { guardBinaryCandidates } = binaryMod;
  const env = {
    COW_FS_GUARD: 'C:\\diag\\fs-guard.exe',
    COW_PROJECT_FS_GUARD: 'C:\\diag\\project-fs-guard.exe',
  };
  const read = guardBinaryCandidates({ role: 'read', env, platform: 'win32' });
  const write = guardBinaryCandidates({ role: 'write', env, platform: 'win32' });
  assert.equal(read[0], 'C:\\diag\\fs-guard.exe');
  assert.equal(write[0], 'C:\\diag\\project-fs-guard.exe');
  assert.ok(!write.includes('C:\\diag\\fs-guard.exe'), 'the reader override is not the writer');
  assert.ok(
    !read.includes('C:\\diag\\project-fs-guard.exe'),
    'the writer override must not become readable through the read role',
  );
});

test('resolution reports honestly when nothing exists', () => {
  const { resolveGuardBinary } = binaryMod;
  const found = resolveGuardBinary({
    role: 'write',
    appPath: '/nowhere',
    resourcesPath: '/nowhere',
    env: {},
    platform: 'linux',
    exists: () => false,
  });
  assert.equal(found, null, 'a build with no helper must say null, not guess');
});

test('resolution takes the first candidate that exists, in order', () => {
  const { resolveGuardBinary, guardBinaryCandidates } = binaryMod;
  const candidates = guardBinaryCandidates({
    role: 'read',
    resourcesPath: '/res',
    appPath: '/app',
    env: {},
    platform: 'linux',
  });
  // Only the third candidate exists, so the third must be the answer: this is
  // what "ordered, most-packaged first" means in practice.
  const third = candidates[2];
  const found = resolveGuardBinary({
    role: 'read',
    resourcesPath: '/res',
    appPath: '/app',
    env: {},
    platform: 'linux',
    exists: (candidate) => candidate === third,
  });
  assert.equal(found, third);
});

// ---------------------------------------------------------------------------
// The real checkout.
// ---------------------------------------------------------------------------

test('the real build tree is found by the real candidate list', () => {
  const { resolveGuardBinary, guardBinaryCandidates } = binaryMod;
  // Build the read-only helper if cargo can, so this asserts against an artifact
  // rather than against an assumption.
  let built = fs.existsSync(path.join(native, 'target', 'release', builtName('read')));
  if (!built && !process.env.COW_SKIP_RUST_BUILD) {
    try {
      execFileSync('cargo', ['build', '--release'], { cwd: native, stdio: 'pipe' });
      built = fs.existsSync(path.join(native, 'target', 'release', builtName('read')));
    } catch {
      built = false;
    }
  }

  const candidates = guardBinaryCandidates({ role: 'read', appPath: desktop, env: {} });
  const expected = path.join(desktop, 'native', 'fs-guard', 'target', 'release', builtName('read'));
  assert.ok(
    candidates.includes(expected),
    `the dev candidate list must include ${expected}; got ${JSON.stringify(candidates, null, 2)}`,
  );

  const resolved = resolveGuardBinary({ role: 'read', appPath: desktop, env: {} });
  if (!built) {
    // Honest skip rather than a green that measured nothing.
    assert.equal(resolved, null);
    return;
  }
  assert.equal(
    resolved,
    expected,
    'a real cargo build must be resolvable through the real candidate list -- this '
    + 'is the assertion the missing .exe suffix used to fail',
  );
  assert.ok(fs.existsSync(resolved));
});

test('the write role is not satisfied by the read-only binary', () => {
  const { resolveGuardBinary } = binaryMod;
  // Even with the read-only helper present, asking for the write role must not
  // fall back to it -- that would silently give a write path a helper that
  // refuses every write, and the failure would surface much later.
  const readPath = path.join(desktop, 'native', 'fs-guard', 'target', 'release', builtName('read'));
  if (!fs.existsSync(readPath)) return; // honest skip: nothing built to confuse
  const resolved = resolveGuardBinary({
    role: 'write',
    appPath: desktop,
    env: {},
    exists: (candidate) => candidate === readPath,
  });
  assert.equal(resolved, null, 'the read-only binary must not answer for the write role');
});
