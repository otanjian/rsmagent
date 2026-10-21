const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { PythonBackend } = require('../desktop/dist/main/python-manager.js');

async function child(t, ignoreTerm = false) {
  const proc = spawn(process.execPath, ['-e',
    `${ignoreTerm ? "process.on('SIGTERM', () => {});" : ''}
     process.stdout.write('ready'); setInterval(() => {}, 1000);`],
  { stdio: ['ignore', 'pipe', 'ignore'] });
  t.after(() => { if (proc.exitCode === null && proc.signalCode === null) proc.kill('SIGKILL'); });
  await once(proc.stdout, 'data');
  return proc;
}

test('restart waits for its old backend to exit before starting another', async (t) => {
  const proc = await child(t);
  const backend = new PythonBackend('/unused');
  backend.process = proc;
  let started = false;
  backend.start = async () => {
    assert.ok(proc.exitCode !== null || proc.signalCode !== null);
    started = true;
  };
  await backend.restart();
  assert.equal(started, true);
});

test('stop waits for actual exit and escalates an ignored SIGTERM', {
  skip: process.platform === 'win32' && 'Windows terminates immediately on SIGTERM',
  timeout: 15000,
}, async (t) => {
  const proc = await child(t, true);
  const backend = new PythonBackend('/unused');
  backend.process = proc;
  await backend.stop();
  assert.equal(proc.signalCode, 'SIGKILL');
});
