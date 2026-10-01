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
  const contractDir = path.join(desktop, 'src', 'main', 'project-execution');
  // ``device-client.ts`` now imports the v2 contract, so a change there has to
  // rebuild this file's output too -- otherwise the test would silently run the
  // previous frame constants.
  const sources = [path.join(srcDir, 'device-client.ts')]
    .concat(fs.readdirSync(contractDir).filter((n) => n.endsWith('.ts'))
      .map((n) => path.join(contractDir, n)));
  const newest = Math.max(...sources.map((file) => fs.statSync(file).mtimeMs));
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

test('bundled backend uses its discovered gateway while remote stays on the configured origin', async () => {
  const local = makeClient({ options: {
    origin: 'http://localhost:9899', allowInsecureLoopback: true,
    localGatewayPort: async () => 23456,
  } });
  local.client.start();
  await sleep(10);
  assert.equal(local.sockets[0].url, 'ws://127.0.0.1:23456/api/desktop/connect');
  assert.equal(local.sockets[0].headers.Authorization, 'Bearer native-token');
  const remote = makeClient({ options: {
    localGatewayPort: async () => assert.fail('remote must not discover loopback'),
  } });
  remote.client.start();
  await sleep(10);
  assert.equal(remote.sockets[0].url, 'wss://console.example.com/api/desktop/connect');
});

test('unavailable local metadata retries without sending a bearer elsewhere', async () => {
  const { client, sockets } = makeClient({ options: {
    origin: 'http://localhost:9899', allowInsecureLoopback: true,
    localGatewayPort: async () => { throw new Error('backend restarting'); },
  } });
  client.start();
  await sleep(10);
  assert.equal(client.currentState, 'reconnecting');
  assert.equal(sockets.length, 0);
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

// -- v2 execution frames (change ``align-...``, tasks 7.1 - 7.7) -----------

/**
 * A v2 ``execute_tool`` frame.
 *
 * Recognised by ``protocol_major`` rather than by ``v``: a v1-only reader must
 * refuse it, so the two channels cannot be confused by a missing field.
 */
function v2Frame(overrides = {}) {
  return {
    type: 'execute_tool',
    protocol_major: 2,
    command_id: 'cmd_v2',
    run_id: 'run_v2',
    tool_call_id: 'call_v2',
    binding_id: 'bind_1',
    workspace_id: 'ws_1',
    device_id: 'dev_1',
    grant_version: 3,
    selection_generation: 1,
    connection_epoch: 'ce_1',
    tool: 'read',
    tool_schema_version: 1,
    arguments: { path: 'a.txt' },
    params_digest: `sha256:${'a'.repeat(64)}`,
    expires_at: '2099-01-01T00:00:00Z',
    ...overrides,
  };
}

test('a v2 execute_tool frame is routed to the execution handler, not the read path', async () => {
  const seen = [];
  const { client, sockets, runs } = makeClient({
    options: {
      runExecution: async (frame) => {
        seen.push(frame);
        return {
          type: 'execution_result', protocol_major: 2,
          command_id: frame.command_id, run_id: frame.run_id,
          tool_call_id: frame.tool_call_id,
          state: 'succeeded', execution_phase: 'succeeded', effects: 'completed',
          started_at: 1, finished_at: 2,
        };
      },
    },
  });
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_1' });
  sockets[0].deliver(v2Frame());
  await sleep(10);

  assert.equal(seen.length, 1);
  assert.equal(seen[0].command_id, 'cmd_v2');
  assert.equal(runs.length, 0, 'a v2 frame is never handed to the v1 reader');
  const reply = sockets[0].sent.at(-1);
  assert.equal(reply.type, 'execution_result');
  assert.equal(reply.protocol_major, 2);
  assert.equal(reply.state, 'succeeded');
  assert.equal(reply.effects, 'completed');
  client.stop();
});

test('a v2 frame under a stale epoch is dropped, not run', async () => {
  const seen = [];
  const { client, sockets } = makeClient({
    options: { runExecution: async (frame) => { seen.push(frame); return {}; } },
  });
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_live' });
  sockets[0].deliver(v2Frame({ connection_epoch: 'ce_stale' }));
  await sleep(10);

  assert.equal(seen.length, 0);
  client.stop();
});

test('a v2 command already running here is not started a second time', async () => {
  let calls = 0;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { client, sockets } = makeClient({
    options: {
      runExecution: async () => { calls += 1; await gate; return { type: 'execution_result' }; },
    },
  });
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_1' });
  sockets[0].deliver(v2Frame());
  sockets[0].deliver(v2Frame());
  await sleep(10);

  assert.equal(calls, 1, 'the second delivery is a redelivery, not a second run');
  release();
  await sleep(10);
  client.stop();
});

test('a v2 execution_status frame is answered from the device journal', async () => {
  const asked = [];
  const { client, sockets } = makeClient({
    options: {
      runExecutionStatus: (frame) => {
        asked.push(frame.command_id);
        return { type: 'execution_status', state: 'running' };
      },
    },
  });
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_1' });
  sockets[0].deliver({
    type: 'execution_status', protocol_major: 2, command_id: 'cmd_v2',
    connection_epoch: 'ce_1',
  });
  await sleep(10);

  assert.deepEqual(asked, ['cmd_v2']);
  assert.deepEqual(sockets[0].sent.at(-1), { type: 'execution_status', state: 'running' });
  client.stop();
});

test('without an execution handler a v2 frame is ignored, never faked', async () => {
  const { client, sockets } = makeClient();
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_1' });
  const before = sockets[0].sent.length;
  sockets[0].deliver(v2Frame());
  await sleep(10);

  assert.equal(sockets[0].sent.length, before, 'no answer is invented');
  client.stop();
});

test('a v2 handler that throws still answers, and claims no effect', async () => {
  const { client, sockets } = makeClient({
    options: { runExecution: async () => { throw new Error('worker died'); } },
  });
  client.start();
  await sleep(10);
  sockets[0].deliver({ v: 1, type: 'hello', epoch: 'ce_1' });
  sockets[0].deliver(v2Frame());
  await sleep(10);

  const reply = sockets[0].sent.at(-1);
  assert.equal(reply.type, 'execution_result');
  assert.equal(reply.state, 'failed');
  assert.equal(reply.effects, 'none', 'a transport failure is not an effect on the project');
  assert.equal(reply.error_code, 'device_error');
  assert.match(reply.error_message, /worker died/);
  client.stop();
});
