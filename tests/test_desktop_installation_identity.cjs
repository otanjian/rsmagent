// Installation identity lives in the main process now (task 2.2 / A01).
//
// These run the real compiled module: creation, repair, legacy adoption and
// atomicity are exercised against a temp user-data directory, without Electron.
//
// Run: node --test tests/test_desktop_installation_identity.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const compiled = path.join(desktop, 'dist', 'main', 'installation-identity.js');

let identity;

function needsBuild() {
  const out = path.join(desktop, 'dist', 'main', 'installation-identity.js');
  if (!fs.existsSync(out)) return true;
  const src = path.join(desktop, 'src', 'main', 'installation-identity.ts');
  return fs.statSync(out).mtimeMs < fs.statSync(src).mtimeMs;
}

before(() => {
  if (needsBuild()) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
  identity = require(compiled);
});

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'cow-install-'));
}

test('a first run creates a valid, persisted id', () => {
  const dir = tmpDir();
  const file = identity.installationIdentityPath(dir);
  const result = identity.loadOrCreateInstallationId({ file });
  assert.match(result.id, /^[A-Za-z0-9_-]{22,128}$/);
  assert.deepEqual(
    { created: result.created, repaired: result.repaired, adopted: result.adopted },
    { created: true, repaired: false, adopted: false },
  );
  const stored = JSON.parse(fs.readFileSync(file, 'utf8'));
  assert.equal(stored.id, result.id);
});

test('a second run reuses the stored id', () => {
  const dir = tmpDir();
  const file = identity.installationIdentityPath(dir);
  const first = identity.loadOrCreateInstallationId({ file });
  const second = identity.loadOrCreateInstallationId({ file });
  assert.equal(second.id, first.id);
  assert.equal(second.created, false);
  assert.equal(second.repaired, false);
});

test('a corrupt file is repaired, not trusted', () => {
  const dir = tmpDir();
  const file = identity.installationIdentityPath(dir);
  fs.writeFileSync(file, '{ not json');
  const result = identity.loadOrCreateInstallationId({ file });
  assert.match(result.id, /^[A-Za-z0-9_-]{22,128}$/);
  assert.equal(result.repaired, true);
  assert.equal(result.created, false);
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).id, result.id);
});

test('a structurally valid file with a bad id is repaired too', () => {
  const dir = tmpDir();
  const file = identity.installationIdentityPath(dir);
  fs.writeFileSync(file, JSON.stringify({ id: 'too-short' }));
  const result = identity.loadOrCreateInstallationId({ file });
  assert.notEqual(result.id, 'too-short');
  assert.equal(result.repaired, true);
});

test('a legacy page id is adopted on the first run only', () => {
  const legacy = identity.newInstallationId();
  const dir = tmpDir();
  const file = identity.installationIdentityPath(dir);
  const first = identity.loadOrCreateInstallationId({ file, legacyId: legacy });
  assert.equal(first.id, legacy);
  assert.equal(first.adopted, true);
  // A later, different legacy value must not replace the stored one.
  const other = identity.newInstallationId();
  const second = identity.loadOrCreateInstallationId({ file, legacyId: other });
  assert.equal(second.id, legacy);
  assert.equal(second.adopted, false);
});

test('an invalid legacy id is ignored and a fresh one is minted', () => {
  const dir = tmpDir();
  const file = identity.installationIdentityPath(dir);
  const result = identity.loadOrCreateInstallationId({ file, legacyId: 'nope' });
  assert.match(result.id, /^[A-Za-z0-9_-]{22,128}$/);
  assert.notEqual(result.id, 'nope');
  assert.equal(result.adopted, false);
});

test('writing leaves no temp file behind', () => {
  const dir = tmpDir();
  const file = identity.installationIdentityPath(dir);
  identity.loadOrCreateInstallationId({ file });
  const leftovers = fs.readdirSync(dir).filter((name) => name.includes('.tmp'));
  assert.deepEqual(leftovers, []);
});

test('the identity file is owner-only on posix', { skip: process.platform === 'win32' }, () => {
  const dir = tmpDir();
  const file = identity.installationIdentityPath(dir);
  identity.loadOrCreateInstallationId({ file });
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
});

test('installationIdentityFor memoises, so a page cannot swap the id', () => {
  const dir = tmpDir();
  const legacy = identity.newInstallationId();
  identity.forgetInstallationIdentity(dir);
  const first = identity.installationIdentityFor(dir, legacy);
  assert.equal(first.id, legacy);
  const other = identity.newInstallationId();
  const second = identity.installationIdentityFor(dir, other);
  assert.equal(second.id, legacy, 'the memoised id wins over a later page value');
  identity.forgetInstallationIdentity(dir);
});
