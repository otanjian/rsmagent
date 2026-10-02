// Desktop local-files grants + bindContext (change tasks 8.5 / 8.6).
//
// Pure-Node modules: no Electron import, so cancel-picker / in-memory grant /
// candidate persistence / generation mismatch can be exercised for real.
//
// Run: node --test tests/test_desktop_local_files.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const compiledDir = path.join(desktop, 'dist', 'main', 'local-files');

let grants;
let candidates;
let bindContext;

function needsBuild() {
  const srcDir = path.join(desktop, 'src', 'main', 'local-files');
  if (!fs.existsSync(srcDir)) return true;
  const sources = fs.readdirSync(srcDir)
    .filter((name) => name.endsWith('.ts'))
    .map((name) => path.join(srcDir, name));
  if (!sources.length) return true;
  const newestSource = Math.max(...sources.map((p) => fs.statSync(p).mtimeMs));
  const outs = ['grants.js', 'candidates.js', 'bind-context.js', 'index.js']
    .map((name) => path.join(compiledDir, name));
  for (const out of outs) {
    if (!fs.existsSync(out)) return true;
    if (fs.statSync(out).mtimeMs < newestSource) return true;
  }
  return false;
}

before(() => {
  if (needsBuild()) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], {
      cwd: desktop,
      stdio: 'pipe',
    });
  }
  grants = require(path.join(compiledDir, 'grants.js'));
  candidates = require(path.join(compiledDir, 'candidates.js'));
  bindContext = require(path.join(compiledDir, 'bind-context.js'));
});

const SCOPE = {
  serverId: 'srv_1',
  userId: 'usr_1',
  tenantId: 'tnt_1',
  deviceId: 'dev_1',
};

// ---------------------------------------------------------------------------
// 8.5 grants
// ---------------------------------------------------------------------------

test('canceling the picker produces no grant', () => {
  assert.equal(grants.pathFromDialogResult({ canceled: true, filePaths: ['/x'] }), null);
  assert.equal(grants.pathFromDialogResult({ canceled: false, filePaths: [] }), null);
  assert.equal(grants.pathFromDialogResult(null), null);
});

test('a selected path activates an in-memory grant with a version', () => {
  const registry = new grants.GrantRegistry();
  const chosen = grants.pathFromDialogResult({
    canceled: false,
    filePaths: ['/Users/me/Documents/reports'],
  });
  assert.equal(chosen, '/Users/me/Documents/reports');
  const publicGrant = registry.activate(
    SCOPE,
    chosen,
    grants.labelFromAbsolutePath(chosen),
  );
  assert.equal(publicGrant.label, 'reports');
  assert.equal(publicGrant.grantVersion, 1);
  assert.equal(publicGrant.deviceId, 'dev_1');
  // The public projection never carries the absolute path.
  assert.equal('absolutePath' in publicGrant, false);
  assert.equal(registry.absolutePathFor(publicGrant.id), chosen);
});

test('re-authorizing the same scope bumps the version and replaces the grant', () => {
  const registry = new grants.GrantRegistry();
  const first = registry.activate(SCOPE, '/tmp/a', 'a');
  const second = registry.activate(SCOPE, '/tmp/b', 'b');
  assert.equal(registry.size, 1);
  assert.notEqual(first.id, second.id);
  assert.ok(second.grantVersion > first.grantVersion);
  assert.equal(registry.absolutePathFor(first.id), null);
  assert.equal(registry.absolutePathFor(second.id), '/tmp/b');
});

test('clear drops every active grant (logout) without touching candidates', () => {
  const registry = new grants.GrantRegistry();
  registry.activate(SCOPE, '/tmp/a', 'a');
  registry.activate({ ...SCOPE, deviceId: 'dev_2' }, '/tmp/c', 'c');
  assert.equal(registry.size, 2);
  registry.clear();
  assert.equal(registry.size, 0);
});

test('a tenant or server change revokes the matching grants', () => {
  const registry = new grants.GrantRegistry();
  registry.activate(SCOPE, '/tmp/a', 'a');
  registry.activate({ ...SCOPE, tenantId: 'tnt_2' }, '/tmp/b', 'b');
  const removed = registry.revokeScope({ tenantId: 'tnt_1' });
  assert.equal(removed, 1);
  assert.equal(registry.size, 1);
  assert.equal(registry.get({ ...SCOPE, tenantId: 'tnt_2' }).label, 'b');
});

test('relative paths are refused as a grant root', () => {
  const registry = new grants.GrantRegistry();
  assert.throws(() => registry.activate(SCOPE, 'relative/path', 'x'));
});

test('the picker message states read-only and on-demand transfer', () => {
  assert.match(grants.PICKER_MESSAGE, /只读/);
  assert.match(grants.PICKER_MESSAGE, /按需/);
});

// ---------------------------------------------------------------------------
// 8.5 candidates
// ---------------------------------------------------------------------------

test('candidates persist absolute paths with 0600 and never auto-activate', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'desk-cand-'));
  const filePath = path.join(dir, 'candidates.json');
  let file = candidates.loadCandidates(filePath);
  assert.equal(file.candidates.length, 0);

  file = candidates.rememberCandidate(file, {
    absolutePath: '/Users/me/Docs',
    label: 'Docs',
    ...SCOPE,
  });
  candidates.saveCandidates(filePath, file);

  const reloaded = candidates.loadCandidates(filePath);
  assert.equal(reloaded.candidates.length, 1);
  assert.equal(reloaded.candidates[0].absolutePath, '/Users/me/Docs');

  if (process.platform !== 'win32') {
    const mode = fs.statSync(filePath).mode & 0o777;
    assert.equal(mode, 0o600);
  }

  // A registry starts empty even when candidates exist: restart ≠ auto-connect.
  const registry = new grants.GrantRegistry();
  assert.equal(registry.size, 0);
  assert.equal(
    candidates.candidatesForScope(reloaded, SCOPE).length,
    1,
  );
});

// ---------------------------------------------------------------------------
// 8.6 bindContext
// ---------------------------------------------------------------------------

test('bindContext refuses a stale generation', async () => {
  const registry = new grants.GrantRegistry();
  const grant = registry.activate(SCOPE, '/tmp/a', 'a');
  const result = await bindContext.bindContext(
    {
      deviceId: SCOPE.deviceId,
      agentId: 'agent_1',
      businessSessionId: 'sess_1',
      contextNonce: 'n'.repeat(22),
      generation: 1,
    },
    { liveGeneration: 2, ...SCOPE },
    grant,
    async () => ({ id: 'bind_x' }),
  );
  assert.equal(result.ok, false);
  assert.equal(result.code, 'stale_context');
});

test('bindContext refuses when there is no active grant', async () => {
  const result = await bindContext.bindContext(
    {
      deviceId: SCOPE.deviceId,
      agentId: 'agent_1',
      businessSessionId: 'sess_1',
      contextNonce: 'n'.repeat(22),
      generation: 1,
    },
    { liveGeneration: 1, ...SCOPE },
    null,
    async () => ({ id: 'bind_x' }),
  );
  assert.equal(result.ok, false);
  assert.equal(result.code, 'grant_revoked');
});

test('bindContext posts and returns the binding id when generation matches', async () => {
  const registry = new grants.GrantRegistry();
  const grant = registry.activate(SCOPE, '/tmp/a', 'a');
  let posted = null;
  const result = await bindContext.bindContext(
    {
      deviceId: SCOPE.deviceId,
      agentId: 'agent_1',
      businessSessionId: 'sess_1',
      contextNonce: 'n'.repeat(22),
      generation: 7,
    },
    { liveGeneration: 7, ...SCOPE },
    grant,
    async (body) => {
      posted = body;
      return { id: 'bind_ok' };
    },
  );
  assert.equal(result.ok, true);
  assert.equal(result.bindingId, 'bind_ok');
  assert.equal(result.generation, 7);
  assert.equal(posted.device_id, SCOPE.deviceId);
  assert.equal(posted.agent_id, 'agent_1');
});

test('scopeChanged detects tenant / account / server flips', () => {
  assert.equal(bindContext.scopeChanged(SCOPE, SCOPE), false);
  assert.equal(
    bindContext.scopeChanged(SCOPE, { ...SCOPE, tenantId: 'other' }),
    true,
  );
  assert.equal(
    bindContext.scopeChanged(SCOPE, { ...SCOPE, userId: 'other' }),
    true,
  );
  assert.equal(
    bindContext.scopeChanged(SCOPE, { ...SCOPE, serverId: 'other' }),
    true,
  );
});
