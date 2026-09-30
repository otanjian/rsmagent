// Workspace folder menu: 「选择本机目录」 on desktop when local-files is open.
//
// The shared console folder chip must offer a native directory picker in the
// desktop container (not the server-disk folder picker). Browser sessions stay
// without that entry.
//
// Run: node --test tests/test_desktop_workspace_menu_frontend.cjs

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');
const consoleJs = read('channel/web/static/js/console.js');
const adapterSource = read('channel/web/static/js/fork/desktop-host.js');
const i18nCore = read('channel/web/static/js/i18n/core.js');
const i18nJs = read('channel/web/static/js/core/i18n.js');

const adapterFactory = vm.runInThisContext(
  `(function (window) {\n${adapterSource}\n})`,
  { filename: 'desktop-host.js' },
);

function bridge(overrides = {}) {
  const calls = [];
  const api = {
    getCapabilities: () => Promise.resolve({
      bridge: '1.0',
      methods: [
        'getCapabilities', 'suspendLocalContext', 'saveArtifact',
        'openExternal', 'onHostEvent', 'chooseWorkspace', 'disconnectWorkspace',
      ],
      localFiles: true,
    }),
    suspendLocalContext: () => Promise.resolve({ suspended: true }),
    saveArtifact: () => Promise.resolve({ saved: true }),
    openExternal: () => Promise.resolve({ opened: true }),
    onHostEvent: () => () => undefined,
    chooseWorkspace: (scope) => {
      calls.push(['chooseWorkspace', scope]);
      return Promise.resolve({
        activated: true,
        grant: { id: 'g1', label: 'reports', grantVersion: 1 },
      });
    },
    disconnectWorkspace: () => Promise.resolve({ revoked: 0 }),
    ...overrides,
  };
  return { api, calls };
}

test('i18n carries the local-directory menu label', () => {
  assert.match(i18nCore, /"ws_sel_local_dir"\s*:\s*"选择本机目录"/);
  assert.match(i18nCore, /"ws_sel_local_dir"\s*:\s*"Choose local folder…"/);
  assert.match(i18nJs, /ws_sel_local_dir:\s*'选择本机目录'/);
  assert.match(i18nJs, /ws_sel_local_dir:\s*'Choose local folder…'/);
});

test('console wires a local-directory menu entry through CowDesktopHost', () => {
  assert.match(consoleJs, /ws_sel_local_dir/);
  assert.match(consoleJs, /wsSelChooseLocalDir/);
  assert.match(consoleJs, /CowDesktopHost\.chooseWorkspace/);
  assert.match(consoleJs, /CowDesktopHost\.canChooseWorkspace/);
  // Must not reach for the raw preload bridge.
  assert.equal(/window\.desktopHost/.test(consoleJs), false);
});

test('CowDesktopHost.canChooseWorkspace is true only when the host opens local files', async () => {
  const window = {
    addEventListener: () => undefined,
    setTimeout: (fn) => { void fn; return 0; },
    open: () => null,
    document: { body: { appendChild() {}, removeChild() {} }, createElement: () => ({}) },
  };
  const { api } = bridge();
  window.desktopHost = api;
  adapterFactory(window);
  assert.equal(typeof window.CowDesktopHost.canChooseWorkspace, 'function');
  assert.equal(await window.CowDesktopHost.canChooseWorkspace(), true);

  const closed = bridge({
    getCapabilities: () => Promise.resolve({
      bridge: '1.0',
      methods: ['getCapabilities', 'suspendLocalContext', 'saveArtifact', 'openExternal', 'onHostEvent'],
      localFiles: false,
    }),
  });
  const window2 = {
    addEventListener: () => undefined,
    setTimeout: (fn) => { void fn; return 0; },
    open: () => null,
    document: { body: { appendChild() {}, removeChild() {} }, createElement: () => ({}) },
    desktopHost: closed.api,
  };
  adapterFactory(window2);
  assert.equal(await window2.CowDesktopHost.canChooseWorkspace(), false);
});

test('a browser cannot choose a local workspace directory', async () => {
  const window = {
    addEventListener: () => undefined,
    setTimeout: (fn) => { void fn; return 0; },
    open: () => null,
    document: { body: { appendChild() {}, removeChild() {} }, createElement: () => ({}) },
  };
  adapterFactory(window);
  assert.equal(await window.CowDesktopHost.canChooseWorkspace(), false);
  const result = await window.CowDesktopHost.chooseWorkspace({ scope: {} });
  assert.equal(result.activated, false);
  assert.equal(result.reason, 'no_host');
});
