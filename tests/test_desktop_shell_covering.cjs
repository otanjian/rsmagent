// Shell covering: parent titlebar drag must release while a full-bleed guest
// WebContentsView owns the window chrome.
//
// Run: node --test tests/test_desktop_shell_covering.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const compiled = path.join(desktop, 'dist', 'main', 'remote', 'shell-covering.js');
const css = fs.readFileSync(path.join(desktop, 'src', 'renderer', 'src', 'index.css'), 'utf8');
const appearance = fs.readFileSync(
  path.join(root, 'channel', 'web', 'static', 'css', 'appearance.css'),
  'utf8',
);
const ipcSrc = fs.readFileSync(
  path.join(desktop, 'src', 'main', 'remote', 'remote-container-ipc.ts'),
  'utf8',
);

let mod;

before(() => {
  const src = path.join(desktop, 'src', 'main', 'remote', 'shell-covering.ts');
  if (!fs.existsSync(compiled) || fs.statSync(compiled).mtimeMs < fs.statSync(src).mtimeMs) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
  mod = require(compiled);
});

test('remoteCoveringScript toggles the documented class', () => {
  assert.equal(mod.REMOTE_COVERING_CLASS, 'cow-remote-covering');
  assert.match(mod.remoteCoveringScript(true), /classList\.toggle\('cow-remote-covering', true\)/);
  assert.match(mod.remoteCoveringScript(false), /classList\.toggle\('cow-remote-covering', false\)/);
});

test('shell CSS disables parent titlebar drag while covered', () => {
  assert.match(css, /html\.cow-remote-covering[\s\S]*-webkit-app-region:\s*no-drag\s*!important/);
  assert.match(css, /html\.cow-remote-covering[\s\S]*pointer-events:\s*none/);
});

test('web workbench header keeps interactive chips no-drag', () => {
  assert.match(
    appearance,
    /#main-content > \.workbench-header[\s\S]*-webkit-app-region:\s*drag/,
  );
  assert.match(
    appearance,
    /#todo-bell-btn[\s\S]*-webkit-app-region:\s*no-drag|#workspace-toggle-btn[\s\S]*-webkit-app-region:\s*no-drag/,
  );
});

test('attach and detach wire shell covering', () => {
  assert.match(ipcSrc, /setShellCovered\(options\.window, true\)/);
  assert.match(ipcSrc, /setShellCovered\(coveredWindow, false\)/);
  assert.match(ipcSrc, /from '\.\/shell-covering'/);
});
