// The port guard has to be honest about a busy port, and conservative about
// whose process it clears.
//
// Change ``harden-windows-desktop-reliability``, task 5.1. The investigation
// into "who started the second backend" ended at the port probe: ``isPortFree``
// bound 127.0.0.1:port and believed the answer, but on Windows a plain bind
// takes SO_REUSEADDR, so it succeeds while another process serves 0.0.0.0:port.
// ``pickPort()`` therefore handed the backend a port it could not use and the
// backend died with WinError 10048 (run.log 2089-2099). The same lie also meant
// ``freePort()`` never ran -- which is the only reason the user's own
// ``python app.py`` survived.
//
// These tests use real sockets against the compiled module, because the point is
// what the OS actually does, not what the source looks like.
//
// Run: node --test tests/test_desktop_backend_port_guard.cjs

const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const compiled = path.join(desktop, 'dist', 'main', 'backend-port.js');
const managerSource = fs.readFileSync(
    path.join(desktop, 'src', 'main', 'python-manager.ts'), 'utf8');

// High and unlikely to be in use. The second one is only ever probed when free.
const PORT = 45999;
const FREE_PORT = 45998;

let mod;
let holder = null;

before(() => {
    const src = path.join(desktop, 'src', 'main', 'backend-port.ts');
    if (!fs.existsSync(compiled) || fs.statSync(compiled).mtimeMs < fs.statSync(src).mtimeMs) {
        execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
    }
    delete require.cache[require.resolve(compiled)];
    mod = require(compiled);
});

after(async () => {
    if (holder) await new Promise((resolve) => holder.close(resolve));
});

/** Hold a port the way the Python backend does: on 0.0.0.0, not loopback. */
function hold(port) {
    return new Promise((resolve, reject) => {
        const server = net.createServer();
        server.once('error', reject);
        server.once('listening', () => resolve(server));
        server.listen(port, '0.0.0.0');
    });
}

test('a busy port is not reported free, even though a bind succeeds', async () => {
    holder = await hold(PORT);
    const answers = await mod.portAnswers(PORT);
    assert.equal(answers, true, 'the connection test has to notice the listener');
    assert.equal(await mod.isPortFree(PORT), false,
        'isPortFree must not hand the backend a port that something is serving');
    if (process.platform === 'win32') {
        // The quirk itself, recorded so a future port of this rule sees it: the
        // bind succeeds on Windows while the port is occupied.
        assert.equal(await mod.canBind(PORT), true,
            'on Windows SO_REUSEADDR lets a specific-address bind succeed on a busy port');
    }
});

test('a port with nothing on it is still free', async () => {
    assert.equal(await mod.isPortFree(FREE_PORT), true);
    assert.equal(await mod.portAnswers(FREE_PORT), false);
});

test('the listener behind a busy port is found by PID', async () => {
    const pids = await mod.listenerPids(PORT);
    assert.ok(pids.includes(process.pid),
        `expected our own listener (${process.pid}) in ${JSON.stringify(pids)}`);
});

test('the image behind a PID is readable', async () => {
    const image = await mod.listenerImage(process.pid);
    assert.ok(image.length > 0, 'tasklist/ps has to answer for a live PID');
    assert.match(image, /node/, `expected a node image, got ${image}`);
});

test('only our own child or our packaged backend may be cleared', () => {
    assert.equal(mod.isOurBackend(4242, 'node.exe', 4242), true, 'the child we spawned is ours');
    assert.equal(mod.isOurBackend(4242, 'cowagent-backend.exe', undefined), true,
        'the executable we ship is ours');
    assert.equal(mod.isOurBackend(4242, 'python.exe', undefined), false,
        'a source-run python is the user\'s process, not ours');
    assert.equal(mod.isOurBackend(4242, 'python.exe', 9999), false);
    assert.equal(mod.isOurBackend(4242, '', undefined), false,
        'an unidentifiable listener is not provably ours');
});

test('the manager clears a listener only after proving it is ours', () => {
    const freePort = /private async freePort[\s\S]*?\n  \}/.exec(managerSource);
    assert.ok(freePort, 'freePort not found in python-manager.ts');
    const body = freePort[0];
    const ownershipChecks = body.match(/isOurBackend\(pid, image, this\.process\?\.pid\)/g) || [];
    assert.equal(ownershipChecks.length, 2,
        'both the SIGTERM and the SIGKILL pass must filter on ownership');
    assert.match(body, /this app did not start — leaving it running/,
        'a foreign listener has to be reported, not silently ignored');
    // \r?\n: the checkout on Windows has CRLF, and an assertion about the
    // manager's logic must not fail because of the file's line endings.
    assert.match(body, /if \(ours\.length === 0\) \{\r?\n\s*return\r?\n\s*\}/,
        'with no listener of ours there is nothing to clear');
});
