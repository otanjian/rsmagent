// Does the desktop's port probe tell the truth on Windows?
//
// Change ``harden-windows-desktop-reliability``, task 5.1. ``isPortFree()`` in
// python-manager.ts decides "can the backend bind this port?" by binding
// 127.0.0.1:port with a Node server. On Windows a plain winsock bind uses
// SO_REUSEADDR by default (Node sets it), so binding a *specific* address
// succeeds while another process holds 0.0.0.0:port -- which is exactly how
// Python's cheroot serves. The probe therefore reports "free", pickPort()
// returns the busy port, and the backend we spawn dies with WinError 10048.
//
// This script reproduces that shape with two sockets in one process:
//
//   1. hold 0.0.0.0:9899 the way the backend does;
//   2. run the probe's own logic (bind 127.0.0.1:9899) and report the verdict;
//   3. run a connect test against 127.0.0.1:9899 and report that verdict too.
//
// Run: node scripts/repro/probe-port-free-windows.cjs

const net = require('node:net');
const path = require('node:path');

const PORT = 9899;

/** The probe as shipped: bind 127.0.0.1:port; success means "free". */
function shippedProbeIsFree(port) {
    return new Promise((resolve) => {
        const tester = net.createServer();
        tester.once('error', () => resolve(false));
        tester.once('listening', () => tester.close(() => resolve(true)));
        tester.listen(port, '127.0.0.1');
    });
}

/** A candidate: a real connection attempt is evidence something is listening. */
function connectTestOccupied(port) {
    return new Promise((resolve) => {
        const socket = net.connect({ port, host: '127.0.0.1' });
        const done = (occupied) => {
            socket.destroy();
            resolve(occupied);
        };
        socket.setTimeout(750);
        socket.once('connect', () => done(true));
        socket.once('timeout', () => done(false));
        socket.once('error', () => done(false));
    });
}

function hold(port, host) {
    return new Promise((resolve, reject) => {
        const holder = net.createServer();
        holder.once('error', reject);
        holder.once('listening', () => resolve(holder));
        holder.listen(port, host);
    });
}

(async () => {
    let holder;
    try {
        holder = await hold(PORT, '0.0.0.0');
    } catch (error) {
        console.log(`RESULT: cannot hold ${PORT}: ${error.code} -- nothing to measure`);
        process.exit(3);
    }
    console.log(`a socket holds 0.0.0.0:${PORT} (the way the backend serves)`);

    const shipped = await shippedProbeIsFree(PORT);
    const occupied = await connectTestOccupied(PORT);
    // The rule as shipped *now* (desktop/src/main/backend-port.ts, compiled):
    // the bind is followed by a connection attempt, so it should agree with the
    // connect test rather than with the old probe.
    let fixed = 'unavailable (compile desktop with tsc -p tsconfig.main.json)';
    try {
        const port = require(path.join(__dirname, '..', '..', 'desktop', 'dist', 'main', 'backend-port.js'));
        fixed = await port.isPortFree(PORT);
    } catch (error) {
        fixed = `unavailable (${error.code || error.message})`;
    }
    console.log(`  shipped probe  (bind 127.0.0.1:${PORT}) says free      : ${shipped}`);
    console.log(`  backend-port.isPortFree (the fix) says free             : ${fixed}`);
    console.log(`  connect test   (dial 127.0.0.1:${PORT}) says occupied  : ${occupied}`);

    const misled = shipped === true && occupied === true;
    console.log(`  the old probe was ${misled ? 'WRONG' : 'right'} about this port`);
    console.log(`RESULT: the fix says the port is ${fixed === false ? 'busy (correct)' : `free (${fixed})`}`);
    process.exitCode = fixed === false ? 0 : 1;

    await new Promise((resolve) => holder.close(resolve));
    process.exit(misled ? 0 : 1);
})();
