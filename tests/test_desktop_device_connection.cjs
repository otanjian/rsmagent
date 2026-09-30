// Device connection allow-list + backoff (change task 9.5).
//
// Run: node --test tests/test_desktop_device_connection.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const compiled = path.join(desktop, 'dist', 'main', 'remote', 'device-connection.js');

let mod;

before(() => {
  const src = path.join(desktop, 'src', 'main', 'remote', 'device-connection.ts');
  if (!fs.existsSync(compiled) || fs.statSync(compiled).mtimeMs < fs.statSync(src).mtimeMs) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
  mod = require(compiled);
});

test('deviceConnectUrl builds wss from an exact https origin', () => {
  assert.equal(
    mod.deviceConnectUrl('https://console.example.com'),
    'wss://console.example.com/api/desktop/connect',
  );
  assert.equal(
    mod.deviceConnectUrl('https://console.example.com:8443'),
    'wss://console.example.com:8443/api/desktop/connect',
  );
});

test('deviceConnectUrl refuses path, query, userinfo', () => {
  assert.throws(() => mod.deviceConnectUrl('https://x.example/path'));
  assert.throws(() => mod.deviceConnectUrl('https://x.example?q=1'));
  assert.throws(() => mod.deviceConnectUrl('https://user:pass@x.example'));
});

test('assertNotArbitraryWsUrl refuses page-supplied ws URLs', () => {
  assert.throws(() => mod.assertNotArbitraryWsUrl('wss://evil.example/ws'));
  assert.throws(() => mod.assertNotArbitraryWsUrl('ws://evil.example/ws'));
  assert.doesNotThrow(() => mod.assertNotArbitraryWsUrl('/api/desktop/connect'));
});

test('reconnect backoff follows the contract table with jitter', () => {
  const delays = [];
  for (let i = 0; i < 8; i++) {
    delays.push(mod.nextReconnectDelayMs(i, () => 0.5)); // zero jitter
  }
  assert.deepEqual(
    delays.slice(0, 6),
    [1000, 2000, 4000, 8000, 16000, 30000],
  );
  assert.equal(delays[7], 30000, 'beyond the table uses the last entry');
});

test('frameFits enforces the 64 KiB bound', () => {
  assert.equal(mod.frameFits('x'.repeat(100)), true);
  assert.equal(mod.frameFits('x'.repeat(64 * 1024 + 1)), false);
});

test('nativeConnectHeaders carry Bearer only', () => {
  const headers = mod.nativeConnectHeaders('tok_abc');
  assert.equal(headers.Authorization, 'Bearer tok_abc');
  assert.equal(headers.Cookie, undefined);
});
