// Diagnostics scrubbing + protocol negotiation (tasks 14.1 / 14.4).
//
// Run: node --test tests/test_desktop_diagnostics.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const desktop = path.join(__dirname, '..', 'desktop');
const compiled = path.join(desktop, 'dist', 'main', 'remote', 'diagnostics.js');

let mod;

before(() => {
  const src = path.join(desktop, 'src', 'main', 'remote', 'diagnostics.ts');
  if (!fs.existsSync(compiled) || fs.statSync(compiled).mtimeMs < fs.statSync(src).mtimeMs) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
  mod = require(compiled);
});

test('scrubDiagnosticsText redacts tokens, cookies and absolute paths', () => {
  const scrubbed = mod.scrubDiagnosticsText(
    'token=abc123 Bearer eyJhbGciOi path=/Users/me/secret.xlsx cookie: sess=1',
  );
  assert.equal(scrubbed.includes('abc123'), false);
  assert.equal(scrubbed.includes('eyJhbGciOi'), false);
  assert.equal(scrubbed.includes('/Users/me/secret.xlsx'), false);
  assert.ok(scrubbed.includes('[redacted]') || scrubbed.includes('[path]'));
});

test('buildDiagnosticsBundle never echoes secrets from notes', () => {
  const bundle = mod.buildDiagnosticsBundle({
    appVersion: '2.1.9',
    bridgeVersion: '1.0',
    connectionCode: 'auth_required',
    correlationIds: ['corr_1'],
    notes: 'failed with token=supersecret at /Users/me/a.txt',
  });
  assert.equal(bundle.appVersion, '2.1.9');
  assert.equal(bundle.notes.includes('supersecret'), false);
  assert.equal(bundle.notes.includes('/Users/me/a.txt'), false);
});

test('negotiateProtocol blocks unknown required major', () => {
  assert.equal(
    mod.negotiateProtocol({ major: 1, minor: 2, required: true }, { major: 2, minor: 0 }).ok,
    false,
  );
  const ok = mod.negotiateProtocol(
    { major: 1, minor: 2, required: true },
    { major: 1, minor: 0 },
  );
  assert.equal(ok.ok, true);
  assert.equal(ok.minor, 0);
});
