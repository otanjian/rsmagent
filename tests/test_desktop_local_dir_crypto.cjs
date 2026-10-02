// Native directory selection: the console's CSPRNG calls must keep their
// receiver, and the installation id must be stable and persisted.
//
// Change align-desktop-project-execution-with-master, tasks 2.1/2.2/A01.
//
// `crypto.getRandomValues` is a method of the crypto object. Reading it into a
// bare reference and calling that (`(crypto.getRandomValues)(bytes)`) passes an
// undefined receiver and Chromium throws `Illegal invocation` -- exactly the
// failure the first directory pick used to hit. These tests execute the real
// helper source under a `crypto` that *enforces* its receiver, instead of only
// grepping for the call shape.
//
// Run: node --test tests/test_desktop_local_dir_crypto.cjs

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const consoleJs = fs.readFileSync(
  path.join(root, 'channel/web/static/js/console.js'), 'utf8');

// The three helpers sit together, between the random-token comment block and
// the grant-scope builder.
const START = 'function _desktopRandomBytes';
const END = 'async function _desktopLocalGrantScope';
const startAt = consoleJs.indexOf(START);
const endAt = consoleJs.indexOf(END);
assert.ok(startAt >= 0 && endAt > startAt, 'helper block found in console.js');
const helperSource = consoleJs.slice(startAt, endAt);

/** A crypto whose getRandomValues rejects a detached receiver, as Chromium does. */
function receiverCheckingCrypto(fill) {
  return {
    getRandomValues(bytes) {
      // Chromium throws `Illegal invocation` when the receiver is not a crypto
      // object; a detached call gets the global (or undefined) instead.
      if (!this || this === undefined || typeof this.getRandomValues !== 'function') {
        throw new TypeError('Illegal invocation');
      }
      if (typeof fill === 'function') return fill(bytes);
      for (let i = 0; i < bytes.length; i++) bytes[i] = (i * 7 + 11) & 0xff;
      return bytes;
    },
  };
}

function sandbox(overrides = {}) {
  const store = new Map();
  const window = {
    crypto: receiverCheckingCrypto(),
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => { store.set(k, String(v)); },
    },
    btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
    Uint8Array,
    ...overrides,
  };
  // `crypto` is a bare global in the helper source, and the receiver check
  // compares against that same binding.
  window.crypto.__name = 'crypto';
  const context = vm.createContext(window);
  vm.runInContext(helperSource, context, { filename: 'console.js#desktop-random' });
  return { window, store, context };
}

test('a truly detached getRandomValues call throws (guards the test itself)', () => {
  const { context } = sandbox();
  const crypto = context.crypto;
  // Parenthesising a member expression keeps its receiver; the bug was the
  // `||` fallback, which evaluates to the bare function value.
  assert.throws(
    () => vm.runInContext(
      '(crypto.getRandomValues || (() => { throw new Error("no crypto"); }))(new Uint8Array(1))',
      context),
    /Illegal invocation/,
  );
  // The correct form works.
  assert.doesNotThrow(() => crypto.getRandomValues(new Uint8Array(1)));
});

test('_desktopRandomBytes keeps the receiver and returns filled bytes', () => {
  const { context } = sandbox();
  const out = vm.runInContext('_desktopRandomBytes(24)', context);
  assert.equal(out.length, 24);
  assert.ok(out.some((b) => b !== 0), 'bytes were actually written');
});

test('_desktopRandomBytes reports a missing CSPRNG instead of inventing bytes', () => {
  const { context } = sandbox({ crypto: {} });
  assert.throws(
    () => vm.runInContext('_desktopRandomBytes(8)', context),
    /crypto unavailable/,
  );
});

test('installation id is persisted and stable across calls', () => {
  const { context, store } = sandbox();
  const first = vm.runInContext('_desktopInstallationId()', context);
  const second = vm.runInContext('_desktopInstallationId()', context);
  assert.equal(first, second);
  assert.match(first, /^[A-Za-z0-9_-]{22,128}$/);
  assert.equal(store.get('cow_desktop_installation_id'), first);
});

test('a corrupt stored id is replaced by a fresh, valid one', () => {
  const { context, store } = sandbox();
  store.set('cow_desktop_installation_id', 'not a valid id!!');
  const fresh = vm.runInContext('_desktopInstallationId()', context);
  assert.match(fresh, /^[A-Za-z0-9_-]{22,128}$/);
  assert.notEqual(fresh, 'not a valid id!!');
  // The replacement is persisted too, so the whole installation keeps one id.
  assert.equal(store.get('cow_desktop_installation_id'), fresh);
  assert.equal(vm.runInContext('_desktopInstallationId()', context), fresh);
});

test('an id that cannot be persisted is refused, not silently minted', () => {
  const throwing = {
    getItem: () => null,
    setItem: () => { throw new Error('quota'); },
  };
  const { context } = sandbox({ localStorage: throwing });
  assert.throws(
    () => vm.runInContext('_desktopInstallationId()', context),
    /quota/,
  );
});

test('console.js no longer detaches getRandomValues', () => {
  assert.equal(/\(crypto\.getRandomValues \|\|/.test(consoleJs), false,
    'the detached-receiver fallback is gone');
  assert.match(consoleJs, /crypto\.getRandomValues\(bytes\)/);
});
