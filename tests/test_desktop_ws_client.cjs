// The device gateway's WebSocket client: handshake, framing, close (task 2.4).
//
// Change ``fix-desktop-local-context-and-tool-calls``. The device connection is
// the only place the app speaks a protocol that is not ``fetch``, so the parts
// that decide whether it works -- the upgrade, masked client frames, ping
// answers, and an unmasked/invalid server frame being refused rather than
// misread -- are driven here against a real ``net`` server, not a mock socket.
//
// Run: node --test tests/test_desktop_ws_client.cjs

const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const dist = path.join(desktop, 'dist', 'main');

let ws;

function needsBuild() {
  const srcDir = path.join(desktop, 'src', 'main', 'remote');
  const sources = ['ws-client.ts'].map((name) => path.join(srcDir, name));
  const newest = Math.max(...sources.map((file) => fs.statSync(file).mtimeMs));
  const out = path.join(dist, 'remote', 'ws-client.js');
  return !fs.existsSync(out) || fs.statSync(out).mtimeMs < newest;
}

before(() => {
  if (needsBuild()) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
  ws = require(path.join(dist, 'remote', 'ws-client.js'));
});

// ---------------------------------------------------------------------------
// pure framing
// ---------------------------------------------------------------------------

test('the accept key matches RFC 6455 §4.2.2', () => {
  // The example from the RFC: this is the value every server must answer with.
  assert.equal(ws.acceptFor('dGhlIHNhbXBsZSBub25jZQ=='), 's3pPLMBiTxaQ9kYGzzhZRbK+xOo=');
});

test('a client text frame is masked and carries the payload', () => {
  const frame = ws.encodeTextFrame('{"v":1}', Buffer.from([1, 2, 3, 4]));
  assert.equal(frame[0], 0x81, 'FIN + text opcode');
  assert.equal(frame[1] & 0x80, 0x80, 'the mask bit must be set: server frames are not masked');
  const length = frame[1] & 0x7f;
  const mask = frame.subarray(2, 6);
  const payload = frame.subarray(6, 6 + length);
  const unmasked = Buffer.alloc(length);
  for (let i = 0; i < length; i += 1) unmasked[i] = payload[i] ^ mask[i % 4];
  assert.equal(unmasked.toString('utf8'), '{"v":1}');
});

test('a payload longer than the contract bound is refused', () => {
  assert.throws(
    () => ws.encodeTextFrame('x'.repeat(ws.FRAME_MAX_BYTES + 1)),
    (err) => err.code === 'frame_too_large',
  );
});

test('decoding waits for a whole frame and survives a split body', () => {
  const frame = Buffer.from([0x81, 0x03, 0x61, 0x62, 0x63]); // "abc", unmasked
  assert.equal(ws.decodeFrame(frame.subarray(0, 3)), null, 'a partial frame is not a frame');
  const decoded = ws.decodeFrame(frame);
  assert.equal(decoded.frame.payload.toString('utf8'), 'abc');
  assert.equal(decoded.rest.length, 0);
});

test('an oversized length prefix is refused, not allocated for', () => {
  const header = Buffer.alloc(10);
  header[0] = 0x81;
  header[1] = 127;
  header.writeBigUInt64BE(BigInt(ws.FRAME_MAX_BYTES + 1), 2);
  assert.throws(() => ws.decodeFrame(header), (err) => err.code === 'frame_too_large');
});

test('a masked server frame is a protocol error', () => {
  const frame = Buffer.from([0x81, 0x83, 1, 2, 3, 4, 0x60, 0x61, 0x62]);
  assert.throws(() => ws.decodeFrame(frame), (err) => err.code === 'invalid_frame');
});

test('a fragmented message is refused rather than half-read', () => {
  const frame = Buffer.from([0x01, 0x03, 0x61, 0x62, 0x63]);
  assert.throws(() => ws.decodeFrame(frame), (err) => err.code === 'invalid_frame');
});

test('an unencrypted gateway is only allowed on loopback', () => {
  assert.throws(
    () => ws.socketOptionsFor('ws://console.example.com/api/desktop/connect', false),
    (err) => err.code === 'downgrade',
  );
  assert.throws(
    () => ws.socketOptionsFor('ws://console.example.com/api/desktop/connect', true),
    (err) => err.code === 'downgrade',
  );
  const local = ws.socketOptionsFor('ws://127.0.0.1:8080/api/desktop/connect', true);
  assert.deepEqual([local.host, local.port, local.tls], ['127.0.0.1', 8080, false]);
  // wss is always acceptable, whatever the caller claims.
  const secure = ws.socketOptionsFor('wss://console.example.com/api/desktop/connect', false);
  assert.deepEqual([secure.host, secure.port, secure.tls], ['console.example.com', 443, true]);
});

// ---------------------------------------------------------------------------
// a real socket: handshake, ping, close
// ---------------------------------------------------------------------------

/** A one-shot gateway: accepts, answers with ``accept``, then plays a script. */
function startFakeGateway(t, script) {
  const seen = { frames: [], closed: false };
  const server = net.createServer((socket) => {
    let buffer = Buffer.alloc(0);
    let upgraded = false;
    socket.on('data', (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      if (!upgraded) {
        const end = buffer.indexOf('\r\n\r\n');
        if (end < 0) return;
        const head = buffer.subarray(0, end).toString('latin1');
        buffer = buffer.subarray(end + 4);
        const key = /sec-websocket-key:\s*(.+)/i.exec(head)[1].trim();
        socket.write(
          'HTTP/1.1 101 Switching Protocols\r\n'
          + 'Upgrade: websocket\r\n'
          + 'Connection: Upgrade\r\n'
          + `Sec-WebSocket-Accept: ${ws.acceptFor(key)}\r\n\r\n`,
        );
        upgraded = true;
        script(socket, seen);
      }
      for (;;) {
        const decoded = decodeClientFrame(buffer);
        if (!decoded) break;
        buffer = decoded.rest;
        seen.frames.push(decoded.frame);
      }
    });
    socket.on('close', () => { seen.closed = true; });
    socket.on('error', () => undefined);
  });
  server.listen(0, '127.0.0.1');
  t.after(() => new Promise((resolve) => server.close(() => resolve())));
  return new Promise((resolve) => {
    server.once('listening', () => resolve({ server, seen, port: server.address().port }));
  });
}

/** An unmasked server text frame (what a gateway sends). */
function serverTextFrame(text) {
  const payload = Buffer.from(text, 'utf8');
  const header = payload.length < 126
    ? Buffer.from([0x81, payload.length])
    : Buffer.concat([Buffer.from([0x81, 126]), (() => {
      const b = Buffer.alloc(2); b.writeUInt16BE(payload.length); return b;
    })()]);
  return Buffer.concat([header, payload]);
}

/**
 * Decode one *client* frame: masked, which ``decodeFrame`` deliberately refuses.
 *
 * A gateway has to unmask what it receives, so the fake one needs its own
 * decoder -- using the client's refusal-path decoder here would be testing the
 * wrong direction.
 */
function decodeClientFrame(buffer) {
  if (buffer.length < 2) return null;
  const opcode = buffer[0] & 0x0f;
  const masked = (buffer[1] & 0x80) !== 0;
  let length = buffer[1] & 0x7f;
  let offset = 2;
  if (length === 126) {
    if (buffer.length < 4) return null;
    length = buffer.readUInt16BE(2);
    offset = 4;
  }
  let mask = null;
  if (masked) {
    if (buffer.length < offset + 4) return null;
    mask = buffer.subarray(offset, offset + 4);
    offset += 4;
  }
  if (buffer.length < offset + length) return null;
  const payload = Buffer.from(buffer.subarray(offset, offset + length));
  if (mask) for (let i = 0; i < payload.length; i += 1) payload[i] ^= mask[i % 4];
  return { frame: { opcode, payload }, rest: buffer.subarray(offset + length) };
}

test('a framed conversation: hello out, text in, ping answered, close observed', async (t) => {
  const { seen, port } = await startFakeGateway(t, (socket, state) => {
    socket.write(serverTextFrame('{"v":1,"type":"hello","epoch":"ce_1"}'));
    socket.write(Buffer.from([0x89, 0x02, 0x68, 0x69])); // ping "hi"
    // A split frame: the client must wait for the rest.
    const tail = serverTextFrame('{"v":1,"type":"command","request_id":"cmd_1"}');
    socket.write(tail.subarray(0, 3));
    setTimeout(() => socket.write(tail.subarray(3)), 10);
    setTimeout(() => {
      socket.write(Buffer.from([0x88, 0x02, 0x03, 0xe8])); // close 1000
      void state;
    }, 40);
  });

  const client = ws.openWebSocket(
    `ws://127.0.0.1:${port}/api/desktop/connect`,
    { headers: { Authorization: 'Bearer native-token' }, allowInsecureLoopback: true },
  );
  const texts = [];
  const closed = new Promise((resolve) => client.onClose((info) => resolve(info)));
  client.onText((text) => texts.push(text));
  await client.opened;
  client.send(JSON.stringify({ v: 1, type: 'hello', device_id: 'dev_1' }));

  const info = await closed;
  assert.equal(info.code, 1000);

  // What we sent is masked and readable.
  const sent = seen.frames.filter((frame) => frame.opcode === 0x1);
  assert.equal(JSON.parse(sent[0].payload.toString('utf8')).type, 'hello');
  // The client answered the ping with a pong echoing the payload.
  const pong = seen.frames.find((frame) => frame.opcode === 0xa);
  assert.equal(pong.payload.toString('utf8'), 'hi');
  // Both complete server messages arrived, including the split one.
  assert.deepEqual(texts.map((text) => JSON.parse(text).type), ['hello', 'command']);
});

test('a refused upgrade rejects instead of looking connected', async (t) => {
  const server = net.createServer((socket) => {
    socket.on('data', () => {
      socket.write('HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n');
    });
  });
  server.listen(0, '127.0.0.1');
  t.after(() => new Promise((resolve) => server.close(() => resolve())));
  const port = await new Promise((resolve) => server.once('listening', () => resolve(server.address().port)));

  const client = ws.openWebSocket(
    `ws://127.0.0.1:${port}/api/desktop/connect`,
    { headers: { Authorization: 'Bearer t' }, allowInsecureLoopback: true },
  );
  await assert.rejects(client.opened, (err) => err.code === 'handshake_failed');
});

test('a handshake without a bearer is refused before connecting', () => {
  const client = ws.openWebSocket('wss://console.example.com/api/desktop/connect', { headers: {} });
  return assert.rejects(client.opened, (err) => err.code === 'invalid_request');
});
