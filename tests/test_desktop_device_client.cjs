// The device connection: hello, commands, epoch fencing, reconnect (task 2.4).
//
// Change ``fix-desktop-local-context-and-tool-calls``. The connection is what
// turns a queued server command into a local read, so the behaviours that
// decide whether that happens are pinned here: the hello precedes any command
// answer, a result carries the *live* epoch, a command under a stale epoch is
// dropped instead of executed, an unimplemented op fails loudly, and a refused
// handshake retries rather than looking connected.
//
// Run: node --test tests/test_desktop_device_client.cjs

const { test, before, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const dist = path.join(desktop, 'dist', 'main');

let clientMod;

function needsBuild() {
  const srcDir = path.join(desktop, 'src', 'main', 'remote');
  const newest = Math.max(
    ...[path.join(srcDir, 'device-client.ts')].map((file) => fs.statSync(file).mtimeMs),
  );
  const out = path.join(dist, 'remote', 'device-client.js');
  return !fs.existsSync(out) || fs.statSync(out).mtimeMs < newest;
}

before(() => {
  if (needsBuild()) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
  clientMod = require(path.join(dist, 'remote', 'device-client.js'));
});

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Every client a test built, so the async ones can always be stopped.
 *
 * A failing assertion would otherwise skip the test's own ``client.stop()`` and
 * leave a reconnect timer armed: the runner then waits on that loop instead of
 * reporting the failure (observed as a two-minute hang, not a red test).
 */
const built = [];

afterEach(() => {
  for (const client of built.splice(0)) client.stop();
});

/**
 * A stand-in socket: it records what the client sends and lets the test push
 * frames back, exactly as the gateway would.
 *
 * A refused handshake is modelled by rejecting ``opened`` and firing close,
 * which is what the real client does when the upgrade never completes.
 */
function fakeSocket(options = {}) {
  const sent = [];
  const textListeners = [];
  const closeListeners = [];
  const socket = {
    sent,
    closed: false,
    closeCode: null,
    headers: options.headers,
    url: options.url,
    opened: options.refuse
      ? Promise.reject(Object.assign(new Error('refused'), { code: options.refuse }))
      : Promise.resolve(),
    send: (text) => {
      sent.push(JSON.parse(text));
    },
    close: (code) => {
      socket.closed = true;
      socket.closeCode = code;
      // Deliberately no listener fan-out: the real client suppresses the
      // transport close once *we* closed the socket, so a close event here
      // would be a fiction that overwrites the reason the client chose.
    },
    onText: (listener) => textListeners.push(listener),
    onClose: (listener) => closeListeners.push(listener),
    deliver: (payload) => {
      for (const listener of textListeners) listener(JSON.stringify(payload));
    },
    drop: (code = 1006) => {
      if (socket.closed) return;
      for (const listener of closeListeners) listener({ code, reason: 'dropped' });
    },
  };
  return socket;
}

/** A client wired to a scripted socket. */
function makeClient(overrides = {}) {
  const sockets = [];
  const states = [];
  const runs = [];
  const client = new clientMod.DeviceClient({
    origin: 'https://console.example.com',
    token: async () => 'native-token',
    deviceId: async () => 'dev_1',
    heartbeatSeconds: 0.02,
    idleTimeoutSeconds: 30,
    random: () => 0.5,
    openSocket: (url, opts) => {
      const socket = fakeSocket({ url, headers: opts.headers, ...(overrides.socket || {}) });
      sockets.push(socket);
      return socket;
    },
    runCommand: async (command) => {
      runs.push(command);
      if (overrides.run) return overrides.run(command);
      return { state: 'succeeded', result: { op: command.op, ok: true } };
    },
    onState: (state, detail) => states.push([state, detail]),
    ...overrides.options,
  });
  built.push(client);
  return { client, sockets, states, runs };
}

test('the hello carries the device id and the bearer, and precedes any answer', async () => {
  const { client, sockets, states } = makeClient();
  client.start();
  await sleep(10);
  assert.equal(sockets.length, 1);
  assert.equal(sockets[0].url, 'https://console.example.com'.replace('https://', 'wss://')
    + '/api/desktop/connect');
  assert.equal(sockets[0].headers.Authorization, 'Bearer native-token');
  assert.deepEqual(sockets[0].sent[0], {
    v: 1,
    type: 'hello',
    device_id: 'dev_1',
    protocol_major: 1,
    capabilities: { files: true },
  });
  assert.equal(client.currentState, 'connecting');

  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_1' });
  assert.equal(client.currentState, 'ready');
  assert.equal(client.currentEpoch, 'ce_1');
  client.stop();
});

test('a command runs and its result carries the live epoch', async () => {
  const { client, sockets, runs } = makeClient();
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_7' });
  sockets[0].deliver({
    v: 1,
    type: 'command',
    request_id: 'dcmd_1',
    connection_epoch: 'ce_7',
    workspace_id: 'ws_1',
    op: 'list',
    params: { limit: 10 },
    deadline: 123,
  });
  await sleep(10);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].op, 'list');
  assert.equal(runs[0].workspace_id, 'ws_1');
  const result = sockets[0].sent.at(-1);
  assert.equal(result.type, 'result');
  assert.equal(result.request_id, 'dcmd_1');
  assert.equal(result.connection_epoch, 'ce_7');
  assert.equal(result.state, 'succeeded');
  assert.deepEqual(result.result, { op: 'list', ok: true });
  client.stop();
});

test('a command under a stale epoch is dropped, not executed', async () => {
  const { client, sockets, runs } = makeClient();
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_new' });
  sockets[0].deliver({
    v: 1,
    type: 'command',
    request_id: 'dcmd_old',
    connection_epoch: 'ce_old',
    op: 'list',
    params: {},
  });
  await sleep(10);
  assert.equal(runs.length, 0, 'a superseded epoch must not run anything');
  assert.equal(sockets[0].sent.filter((frame) => frame.type === 'result').length, 0);
  client.stop();
});

test('the same request id is not answered twice', async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { client, sockets, runs } = makeClient({
    run: async (command) => {
      await gate;
      return { state: 'succeeded', result: { op: command.op } };
    },
  });
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_1' });
  const command = {
    v: 1,
    type: 'command',
    request_id: 'dcmd_same',
    connection_epoch: 'ce_1',
    op: 'list',
    params: {},
  };
  sockets[0].deliver(command);
  sockets[0].deliver(command);
  release();
  await sleep(20);
  assert.equal(runs.length, 1);
  client.stop();
});

test('a failing op answers failed with its own code, never an empty success', async () => {
  const { client, sockets } = makeClient({
    run: async () => ({ state: 'failed', errorCode: 'path_outside_root', errorMessage: 'nope' }),
  });
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_1' });
  sockets[0].deliver({
    v: 1, type: 'command', request_id: 'dcmd_x', connection_epoch: 'ce_1', op: 'read_text', params: {},
  });
  await sleep(10);
  const result = sockets[0].sent.at(-1);
  assert.equal(result.state, 'failed');
  assert.equal(result.error_code, 'path_outside_root');
  assert.equal(result.result, undefined);
  client.stop();
});

test('a thrown runner still answers, so the caller is not left waiting', async () => {
  const { client, sockets } = makeClient({
    run: async () => {
      throw new Error('the helper died');
    },
  });
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_1' });
  sockets[0].deliver({
    v: 1, type: 'command', request_id: 'dcmd_y', connection_epoch: 'ce_1', op: 'stat', params: {},
  });
  await sleep(10);
  const result = sockets[0].sent.at(-1);
  assert.equal(result.state, 'failed');
  assert.equal(result.error_code, 'device_error');
  client.stop();
});

test('a hello without an epoch is not a usable connection', async () => {
  const { client, sockets, states } = makeClient();
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello' });
  await sleep(10);
  assert.equal(client.currentEpoch, '');
  assert.equal(client.currentState, 'reconnecting');
  assert.equal(sockets[0].closed, true);
  assert.ok(states.some(([state, detail]) => state === 'reconnecting' && detail === 'hello_without_epoch'));
  client.stop();
});

test('no device id is a configuration problem, not a retry loop', async () => {
  const { client, sockets, states } = makeClient({
    options: { deviceId: async () => null, token: async () => 't' },
  });
  client.start();
  await sleep(20);
  assert.equal(sockets.length, 0);
  assert.equal(client.currentState, 'idle');
  assert.ok(states.some(([state, detail]) => state === 'idle' && /no registered device id/.test(detail)));
  client.stop();
});

test('a refused handshake schedules a reconnect instead of looking ready', async () => {
  const { client, sockets, states } = makeClient({ socket: { refuse: 'handshake_failed' } });
  client.start();
  await sleep(20);
  assert.equal(client.currentState, 'reconnecting');
  assert.ok(states.some(([, detail]) => /handshake_failed/.test(detail || '')));
  // The retry is scheduled on the contract's backoff (1s for the first
  // attempt), so nothing has connected yet.
  assert.equal(sockets.length, 1);
  client.stop();
});

test('a dropped connection reconnects with a fresh hello', async () => {
  const { client, sockets } = makeClient();
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_1' });
  sockets[0].drop(1006);
  assert.equal(client.currentState, 'reconnecting');
  await sleep(1300);
  assert.equal(sockets.length, 2, 'the retry opens a new socket');
  assert.equal(sockets[1].sent[0].type, 'hello');
  assert.equal(client.currentState, 'connecting');
  client.stop();
});

test('stop closes the socket and sends nothing further', async () => {
  const { client, sockets, states } = makeClient();
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_1' });
  client.stop();
  assert.equal(sockets[0].closed, true);
  assert.equal(sockets[0].closeCode, 1000);
  const sentBefore = sockets[0].sent.length;
  sockets[0].deliver({
    v: 1, type: 'command', request_id: 'dcmd_z', connection_epoch: 'ce_1', op: 'list', params: {},
  });
  await sleep(10);
  assert.equal(sockets[0].sent.length, sentBefore);
  assert.deepEqual(states.at(-1), ['stopped', undefined]);
});

test('starting twice opens one connection', async () => {
  const { client, sockets } = makeClient();
  client.start();
  client.start();
  await sleep(10);
  assert.equal(sockets.length, 1);
  client.stop();
});

test('heartbeats continue while the connection is live', async () => {
  const { client, sockets } = makeClient();
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_1' });
  await sleep(60);
  const heartbeats = sockets[0].sent.filter((frame) => frame.type === 'heartbeat');
  assert.ok(heartbeats.length >= 1, 'the lease is kept alive');
  assert.equal(heartbeats[0].connection_epoch, 'ce_1');
  client.stop();
});

test('silence past the idle bound reconnects instead of hanging half-open', async () => {
  const { client, sockets, states } = makeClient({ options: { idleTimeoutSeconds: 0.01 } });
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_1' });
  await sleep(40);
  assert.equal(sockets[0].closed, true, 'the silent socket is closed');
  assert.equal(client.currentState, 'reconnecting');
  assert.ok(states.some(([state, detail]) => state === 'reconnecting' && detail === 'idle_timeout'));
  client.stop();
});
