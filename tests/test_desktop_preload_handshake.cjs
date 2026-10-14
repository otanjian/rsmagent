// The preload handshakes once, and a document that failed to handshake stays
// unable to act -- it must not quietly re-handshake its way past the guard.
//
// Change ``harden-windows-desktop-reliability``, task 9.2, on top of the
// upstream fix task 9.1 records ("一次加载只推进一代"). 9.2 asks for a fresh
// verification on the current build and is explicit that the historical 9.1
// evidence does not stand in for it: "保留 preload 握手不自动重握手绕过守卫".
//
// The guard itself (``checkSender`` refusing a stale frame or generation) is
// driven for real by ``tests/test_desktop_remote_host.cjs`` -- including the
// "load completion is not a new document" case. What had no test was the other
// half: the *preload* side, where a retry loop would have made the guard
// meaningless, because a document that had been refused could simply ask again
// until it was accepted.
//
// Run: node --test tests/test_desktop_preload_handshake.cjs

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const preload = fs.readFileSync(path.join(root, 'desktop', 'src', 'main', 'remote-preload.ts'), 'utf8');
const hostIpc = fs.readFileSync(
    path.join(root, 'desktop', 'src', 'main', 'remote', 'remote-host-ipc.ts'), 'utf8');

test('the handshake is memoised: one attempt per document, no retry loop', () => {
    assert.match(preload, /if \(!handshake\) \{/,
        'the handshake must be created once and reused');
    assert.match(preload, /handshake = ipcRenderer\s*\n?\s*\.invoke\(CHANNEL_HELLO\)/,
        'the one attempt is the hello invoke');
    assert.ok(!/retry|backoff|attempt/i.test(preload),
        'there is no retry vocabulary in the preload');
    assert.ok(!/setInterval|setTimeout/.test(preload),
        'and nothing schedules a second attempt later');
});

test('a failed or refused handshake leaves the document at generation 0', () => {
    assert.match(preload,
        /generation = reply && reply\.ok && typeof reply\.generation === 'number' \? reply\.generation : 0/,
        'only an accepted handshake hands the document a real generation');
    assert.match(preload, /\.catch\(\(\) => \{\s*\n\s*generation = 0/,
        'a rejected invoke leaves generation 0 rather than an optimistic value');
});

test('every call carries the generation the main process handed out', () => {
    assert.match(preload,
        /ipcRenderer\.invoke\(CHANNEL_CALL, \{ method, params: params \|\| \{\}, generation \}\)/,
        'a call must be stamped, or the main process could not refuse a stale one');
});

test('the main process answers the generation only for the registered document', () => {
    const handshake = /function handshake\([\s\S]*?\n\}/.exec(hostIpc);
    assert.ok(handshake, 'the main-side handshake was not found');
    assert.ok(/checkSender\(/.test(handshake[0]),
        'the handshake must run the same sender check as every other call');
    assert.ok(/return \{ ok: true, generation: context\.generation \}/.test(handshake[0]),
        'and answer the live generation only on success');
});
