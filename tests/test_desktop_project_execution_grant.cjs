// "Open my project here" is a distinct authorization from a read-only local
// directory reference (change align-desktop-project-execution-with-master).
//
// The purpose is stored on the grant, stated in the picker, validated at the
// bridge boundary, and *never* inferred: an older caller that omits it keeps the
// read-only reference, and an unknown value is refused rather than assumed.
//
// Run: node --test tests/test_desktop_project_execution_grant.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const compiledMain = path.join(desktop, 'dist', 'main');

let grants;
let bridge;
let hostBridge;
let candidates;
// The contract supplies the tool-to-operation mapping; importing it rather than
// restating the table is the point of the test that uses it.
let contract;

function needsBuild() {
  const outputs = [
    'local-files/grants.js',
    'local-files/candidates.js',
    'remote/local-files-bridge.js',
    'remote/host-bridge.js',
  ].map((p) => path.join(compiledMain, p));
  const sources = [
    'src/main/local-files/grants.ts',
    'src/main/remote/local-files-bridge.ts',
    'src/main/remote/host-bridge.ts',
  ].map((p) => path.join(desktop, p));
  for (const out of outputs) {
    if (!fs.existsSync(out)) return true;
    const newestSource = Math.max(...sources.map((p) => fs.statSync(p).mtimeMs));
    if (fs.statSync(out).mtimeMs < newestSource) return true;
  }
  return false;
}

before(() => {
  if (needsBuild()) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe', shell: process.platform === 'win32',});
  }
  grants = require(path.join(compiledMain, 'local-files', 'grants.js'));
  candidates = require(path.join(compiledMain, 'local-files', 'candidates.js'));
  bridge = require(path.join(compiledMain, 'remote', 'local-files-bridge.js'));
  hostBridge = require(path.join(compiledMain, 'remote', 'host-bridge.js'));
  contract = require(path.join(compiledMain, 'project-execution', 'contract.js'));
});

const SCOPE = { serverId: 'srv_1', userId: 'usr_1', tenantId: 'tnt_1', deviceId: 'dev_1' };
const emptyCandidates = () => ({ version: 1, candidates: [] });

test('an omitted purpose keeps the read-only reference (no silent widening)', () => {
  const registry = new grants.GrantRegistry();
  const grant = registry.activate(SCOPE, '/tmp/legacy', 'legacy');
  assert.equal(grant.purpose, 'readonly-input');
  assert.equal(grants.allowsProjectExecution(grant), false);
});

test('an explicit project-execution grant is stored and projected', () => {
  const registry = new grants.GrantRegistry();
  const grant = registry.activate(SCOPE, '/tmp/proj', 'proj', 'project-execution');
  assert.equal(grant.purpose, 'project-execution');
  assert.equal(grants.allowsProjectExecution(grant), true);
  assert.equal(registry.get(SCOPE).purpose, 'project-execution');
});

test('an unknown purpose is refused, not coerced', () => {
  const registry = new grants.GrantRegistry();
  assert.throws(() => registry.activate(SCOPE, '/tmp/x', 'x', 'write-everything'));
});

test('re-authorizing replaces both the version and the purpose', () => {
  const registry = new grants.GrantRegistry();
  const readOnly = registry.activate(SCOPE, '/tmp/a', 'a', 'readonly-input');
  const exec = registry.activate(SCOPE, '/tmp/a', 'a', 'project-execution');
  assert.ok(exec.grantVersion > readOnly.grantVersion);
  assert.equal(registry.get(SCOPE).purpose, 'project-execution');
});

test('the two purposes have distinct, accurate picker messages', () => {
  assert.match(grants.PICKER_MESSAGE, /只读/);
  assert.match(grants.PROJECT_EXECUTION_PICKER_MESSAGE, /原地执行/);
  assert.equal(/只读/.test(grants.PROJECT_EXECUTION_PICKER_MESSAGE), false);
  assert.equal(grants.pickerMessageFor('readonly-input'), grants.PICKER_MESSAGE);
  assert.equal(
    grants.pickerMessageFor('project-execution'),
    grants.PROJECT_EXECUTION_PICKER_MESSAGE,
  );
});

test('normalizeGrantPurpose maps anything unknown back to read-only', () => {
  assert.equal(bridge.normalizeGrantPurpose('project-execution'), 'project-execution');
  assert.equal(bridge.normalizeGrantPurpose('readonly-input'), 'readonly-input');
  assert.equal(bridge.normalizeGrantPurpose(undefined), 'readonly-input');
  assert.equal(bridge.normalizeGrantPurpose('nonsense'), 'readonly-input');
});

test('applyChooseWorkspace records the requested purpose on the grant', () => {
  const registry = bridge.remoteGrantRegistry;
  const file = emptyCandidates();
  const result = bridge.applyChooseWorkspace({
    scope: SCOPE,
    dialogResult: { canceled: false, filePaths: ['/Users/me/项目 x'] },
    candidates: file,
    purpose: 'project-execution',
  });
  assert.equal(result.ok, true);
  assert.equal(result.grant.purpose, 'project-execution');
  assert.equal(registry.get(SCOPE).purpose, 'project-execution');
  // The absolute path is remembered as a candidate, still never auto-activated.
  assert.equal(file.candidates.length, 1);
  assert.equal(file.candidates[0].absolutePath, '/Users/me/项目 x');
  assert.equal(candidates.candidatesForScope(file, SCOPE).length, 1);
});

test('applyChooseWorkspace without a purpose stays read-only', () => {
  const scope = { ...SCOPE, deviceId: 'dev_2' };
  const result = bridge.applyChooseWorkspace({
    scope,
    dialogResult: { canceled: false, filePaths: ['/tmp/readonly'] },
    candidates: emptyCandidates(),
  });
  assert.equal(result.ok, true);
  assert.equal(result.grant.purpose, 'readonly-input');
});

test('a cancelled pick activates nothing, whatever the purpose', () => {
  const scope = { ...SCOPE, deviceId: 'dev_3' };
  const result = bridge.applyChooseWorkspace({
    scope,
    dialogResult: { canceled: true, filePaths: ['/tmp/never'] },
    candidates: emptyCandidates(),
    purpose: 'project-execution',
  });
  assert.deepEqual(result, { ok: true, activated: false, reason: 'cancelled' });
  assert.equal(bridge.remoteGrantRegistry.get(scope), null);
});

test('the bridge boundary refuses an unknown purpose and accepts the two known', () => {
  const bad = hostBridge.checkBridgeCall({
    method: 'chooseWorkspace',
    params: { scope: SCOPE, purpose: 'sudo' },
  });
  assert.equal(bad.ok, false);
  assert.equal(bad.code, 'invalid_request');

  for (const purpose of ['readonly-input', 'project-execution']) {
    const ok = hostBridge.checkBridgeCall({
      method: 'chooseWorkspace',
      params: { scope: SCOPE, purpose },
    });
    assert.equal(ok.ok, true, `purpose ${purpose} must be accepted`);
  }
  // No purpose at all is still the historical, read-only call.
  assert.equal(hostBridge.checkBridgeCall({ method: 'chooseWorkspace', params: { scope: SCOPE } }).ok, true);
});

// ---------------------------------------------------------------------------
// Task 4.1: the operations a grant authorizes, as the v3 contract names them.
//
// v2 had one permission ("bound ⇒ any allow-listed tool may run"), so
// "open for editing" and "open for running code in" were the same thing. v3
// splits them into ``files`` and ``scripts``, and the split only means anything if
// the grant carries a *set* rather than a boolean.
// ---------------------------------------------------------------------------

test('a read-only grant authorizes no v3 operation at all', () => {
  const registry = new grants.GrantRegistry();
  const grant = registry.activate(SCOPE, '/tmp/read', 'read');
  // An empty set, not "unrestricted": the contract reads absent/empty as "no new
  // operation authorized", and the directory is a read reference rather than a
  // project.
  assert.deepEqual(grants.operationsFor(grant.purpose), []);
  assert.equal(grants.authorizesOperation(grant, 'files'), false);
  assert.equal(grants.authorizesOperation(grant, 'scripts'), false);
});

test('a project-execution grant authorizes files, and NOT scripts', () => {
  const registry = new grants.GrantRegistry();
  const grant = registry.activate(SCOPE, '/tmp/proj', 'proj', 'project-execution');
  assert.deepEqual(grants.operationsFor(grant.purpose), ['files']);
  assert.equal(grants.authorizesOperation(grant, 'files'), true);
  // The one capability whose entire design exists to keep it separate: running
  // code in the user's directory needs its own confirmation, so opening a project
  // for the file tools must never imply it.
  assert.equal(grants.authorizesOperation(grant, 'scripts'), false,
    'opening a project for editing must not authorize running code in it');
});

test('an absent operations field derives from the purpose', () => {
  // Every record written before this change has no ``operations``: deriving is
  // what keeps them working, and it must derive the *narrow* answer.
  assert.equal(grants.authorizesOperation({ purpose: 'readonly-input' }, 'files'), false);
  assert.equal(grants.authorizesOperation({ purpose: 'project-execution' }, 'files'), true);
  assert.equal(grants.authorizesOperation(null, 'files'), false);
});

test('a recorded operations set is the authority, not the purpose', () => {
  // A grant confirmed with a narrower set than its purpose implies must not be
  // widened by re-deriving from the purpose -- that is the silent upgrade v3
  // exists to prevent.
  const narrowed = { purpose: 'project-execution', operations: [] };
  assert.equal(grants.authorizesOperation(narrowed, 'files'), false,
    'the recorded set wins even when it is narrower than the purpose');
  const scripted = { purpose: 'project-execution', operations: ['files', 'scripts'] };
  assert.equal(grants.authorizesOperation(scripted, 'scripts'), true);
});

test('the tool-to-operation mapping comes from the contract, not from a copy', () => {
  // Generated from the contract's ``required_for`` table, so it cannot drift from
  // the document both ends check against.
  assert.equal(contract.requiredOperationForTool('read'), 'files');
  assert.equal(contract.requiredOperationForTool('write'), 'files');
  assert.equal(contract.requiredOperationForTool('edit'), 'files');
  assert.equal(contract.requiredOperationForTool('ls'), 'files');
  assert.equal(contract.requiredOperationForTool('search_files'), 'files');
  assert.equal(contract.requiredOperationForTool('run_script'), 'scripts');
  assert.equal(contract.requiredOperationForTool('bash'), 'scripts');
  // A tool no contract knows needs nothing -- so it is covered by nothing.
  assert.equal(contract.requiredOperationForTool('rm_rf'), undefined);
  assert.equal(contract.coversTool(['files', 'scripts'], 'rm_rf'), false);
});

test('a files-only grant covers the file tools and refuses the script tools', () => {
  // This is the separation task 4.1 is about, stated as the check the execution
  // path now makes: one grant answers differently per tool.
  const held = grants.operationsFor('project-execution');
  assert.equal(contract.coversTool(held, 'read'), true);
  assert.equal(contract.coversTool(held, 'write'), true);
  assert.equal(contract.coversTool(held, 'run_script'), false,
    'opening a project for editing must not authorize running code in it');
  assert.equal(contract.coversTool(held, 'bash'), false);

  // A read-only grant covers nothing at all, which is why no operation name
  // appears for it.
  const none = grants.operationsFor('readonly-input');
  assert.equal(contract.coversTool(none, 'read'), false);
  assert.equal(contract.coversTool(none, 'bash'), false);
  // An absent set is not "unrestricted".
  assert.equal(contract.coversTool(undefined, 'read'), false);
});
