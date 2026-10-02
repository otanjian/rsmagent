// Desktop lifecycle preferences + notification click gates (tasks 13.1–13.4).
//
// Run: node --test tests/test_desktop_lifecycle.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const compiled = path.join(desktop, 'dist', 'main', 'remote', 'lifecycle.js');

let mod;

before(() => {
  const src = path.join(desktop, 'src', 'main', 'remote', 'lifecycle.ts');
  if (!fs.existsSync(compiled) || fs.statSync(compiled).mtimeMs < fs.statSync(src).mtimeMs) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
  mod = require(compiled);
});

test('default close behavior is tray (驻留) and autostart is off', () => {
  assert.deepEqual(mod.DEFAULT_LIFECYCLE_PREFERENCES, {
    closeBehavior: 'tray',
    launchAtLogin: false,
  });
  assert.equal(
    mod.shouldCloseToTray(mod.DEFAULT_LIFECYCLE_PREFERENCES, { isQuitting: false }),
    true,
  );
  assert.equal(
    mod.shouldCloseToTray(mod.DEFAULT_LIFECYCLE_PREFERENCES, { isQuitting: true }),
    false,
  );
  assert.equal(
    mod.shouldCloseToTray({ closeBehavior: 'quit', launchAtLogin: false }, { isQuitting: false }),
    false,
  );
});

test('wake policy revalidates on resume/unlock and ignores suspend', () => {
  assert.deepEqual(mod.wakePolicy('resume'), { action: 'revalidate', reason: 'resume' });
  assert.deepEqual(mod.wakePolicy('unlock-screen'), {
    action: 'revalidate',
    reason: 'unlock-screen',
  });
  assert.deepEqual(mod.wakePolicy('suspend'), { action: 'noop' });
});

test('notification click refuses cross-identity refs', () => {
  const ref = {
    notification_ref: 'nref_1',
    serverId: 'srv_a',
    userId: 'usr_1',
    tenantId: 'tnt_1',
    resourceKind: 'scheduler_run',
    resourceId: 'run_9',
    summary: '任务已完成',
  };
  assert.equal(
    mod.verdictForNotificationClick(ref, {
      activeServerId: 'srv_a',
      activeUserId: 'usr_1',
      activeTenantId: 'tnt_1',
    }).ok,
    true,
  );
  assert.equal(
    mod.verdictForNotificationClick(ref, {
      activeServerId: 'srv_b',
      activeUserId: 'usr_1',
      activeTenantId: 'tnt_1',
    }).code,
    'stale_context',
  );
  assert.equal(
    mod.verdictForNotificationClick(ref, {
      activeServerId: 'srv_a',
      activeUserId: 'usr_other',
      activeTenantId: 'tnt_1',
    }).code,
    'stale_context',
  );
});

test('notification dedupe key is server+user+resource', () => {
  const key = mod.notificationDedupeKey({
    notification_ref: 'nref_1',
    serverId: 's',
    userId: 'u',
    resourceKind: 'agent_run',
    resourceId: 'r1',
    summary: 'ok',
  });
  assert.equal(key, ['s', 'u', 'agent_run', 'r1'].join('\u0001'));
});

test('path-bearing notification summaries are refused', () => {
  const verdict = mod.verdictForNotificationClick(
    {
      notification_ref: 'nref_2',
      serverId: 's',
      userId: 'u',
      resourceKind: 'agent_run',
      resourceId: 'r2',
      summary: '/Users/me/secret.xlsx finished',
    },
    { activeServerId: 's', activeUserId: 'u', activeTenantId: null },
  );
  assert.equal(verdict.ok, false);
  assert.equal(verdict.code, 'unsafe_path');
});
