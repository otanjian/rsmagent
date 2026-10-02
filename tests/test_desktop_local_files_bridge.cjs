// Local-files bridge decisions + save-as applicability (task 12.1 / 12.3).
//
// Run: node --test tests/test_desktop_local_files_bridge.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const compiled = path.join(desktop, 'dist', 'main', 'remote', 'local-files-bridge.js');

let mod;

before(() => {
  const src = path.join(desktop, 'src', 'main', 'remote', 'local-files-bridge.ts');
  if (!fs.existsSync(compiled) || fs.statSync(compiled).mtimeMs < fs.statSync(src).mtimeMs) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
  // Fresh module so the in-memory grant registry starts empty.
  delete require.cache[require.resolve(compiled)];
  mod = require(compiled);
  mod.remoteGrantRegistry.clear();
});

const SCOPE = {
  serverId: 'srv_1',
  userId: 'usr_1',
  tenantId: 'tnt_1',
  deviceId: 'dev_1',
};

test('canceling the picker produces no grant', () => {
  const candidates = { version: 1, candidates: [] };
  const result = mod.applyChooseWorkspace({
    scope: SCOPE,
    dialogResult: { canceled: true, filePaths: ['/tmp/x'] },
    candidates,
  });
  assert.equal(result.ok, true);
  assert.equal(result.activated, false);
  assert.equal(mod.remoteGrantRegistry.size, 0);
  assert.equal(candidates.candidates.length, 0);
});

test('choosing a directory activates a grant and remembers a candidate', () => {
  const candidates = { version: 1, candidates: [] };
  const result = mod.applyChooseWorkspace({
    scope: SCOPE,
    dialogResult: { canceled: false, filePaths: ['/Users/me/Documents/reports'] },
    candidates,
  });
  assert.equal(result.ok, true);
  assert.equal(result.activated, true);
  assert.equal(result.grant.label.includes('reports') || result.grant.label.length > 0, true);
  assert.equal(typeof result.grant.absolutePath, 'undefined');
  assert.equal(mod.remoteGrantRegistry.size, 1);
  assert.equal(candidates.candidates.length, 1);
  assert.equal(candidates.candidates[0].absolutePath, '/Users/me/Documents/reports');
});

test('disconnect clears the active grant without deleting candidates', () => {
  const candidates = { version: 1, candidates: [] };
  mod.applyChooseWorkspace({
    scope: SCOPE,
    dialogResult: { canceled: false, filePaths: ['/Users/me/Docs'] },
    candidates,
  });
  const disconnected = mod.applyDisconnectWorkspace(SCOPE);
  assert.equal(disconnected.revoked, 1);
  assert.equal(mod.remoteGrantRegistry.size, 0);
  assert.equal(candidates.candidates.length, 1, 'candidates survive disconnect');
});

test('save-as approval is not_applicable with an audit reason', () => {
  const applicability = mod.saveAsApprovalApplicability();
  assert.equal(applicability.applicable, false);
  assert.equal(applicability.action, 'desktop.save_as');
  assert.ok(applicability.reason.includes('not an enterprise'));
});

test('automatic write-back is refused', () => {
  const refused = mod.refuseAutomaticWriteBack();
  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'permission_denied');
});

test('capabilities expose chooseWorkspace only when local-files is enabled', () => {
  mod.setRemoteLocalFilesEnabled(false);
  const closed = mod.bridgeCapabilitiesPayload({
    bridge: '1.0', generation: 3, saveAsApproval: { applicable: false },
  });
  assert.equal(closed.localFiles, false);
  assert.equal(closed.methods.includes('chooseWorkspace'), false);

  mod.setRemoteLocalFilesEnabled(true);
  const open = mod.bridgeCapabilitiesPayload({
    bridge: '1.0', generation: 3, saveAsApproval: { applicable: false },
  });
  assert.equal(open.localFiles, true);
  assert.equal(open.methods.includes('chooseWorkspace'), true);
  mod.setRemoteLocalFilesEnabled(false);
});
