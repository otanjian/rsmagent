// Choosing the Python interpreter for the local backend.
//
// Change ``harden-windows-desktop-reliability``, task 2.1. The old search
// returned the first path that *existed* and otherwise the bare string
// ``python``. On Windows that produced "The app exited during startup (code
// 9009)" and no way to tell why: the checkout's ``.venv`` was built on macOS (no
// ``Scripts/`` at all) and PATH's ``python`` is the Microsoft Store placeholder,
// which exists, runs, and refuses to be an interpreter.
//
// So these tests are about the rule that replaced it: a candidate is chosen only
// after it has *answered*, an explicit choice never silently falls back, and a
// probe that hangs is killed instead of left behind.
//
// Run: node --test tests/test_desktop_python_probe.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const srcDir = path.join(desktop, 'src', 'main');
const compiled = path.join(desktop, 'dist', 'main', 'python-probe.js');

let probe;

function needsBuild() {
    if (!fs.existsSync(compiled)) return true;
    const newest = Math.max(
        ...fs.readdirSync(srcDir).filter((n) => n.endsWith('.ts'))
            .map((n) => fs.statSync(path.join(srcDir, n)).mtimeMs),
    );
    return newest > fs.statSync(compiled).mtimeMs;
}

before(() => {
    if (needsBuild()) {
        execFileSync(process.execPath, [
            path.join(desktop, 'node_modules', 'typescript', 'bin', 'tsc'),
            '-p', 'tsconfig.main.json',
        ], { cwd: desktop, stdio: 'inherit' });
    }
    probe = require(compiled);
});

// --- a fake child process, so every failure mode is reachable on any platform --

function answer(version, executable = '/usr/bin/python3') {
    const [major, minor, patch] = version;
    return JSON.stringify({ v: [major, minor, patch], e: executable });
}

function fakeChild(spec) {
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.killed = false;
    child.kill = () => {
        child.killed = true;
        child.emit('close', null);
    };
    setImmediate(() => {
        if (spec.stdout) child.stdout.emit('data', Buffer.from(spec.stdout));
        if (spec.stderr) child.stderr.emit('data', Buffer.from(spec.stderr));
        if (spec.error) child.emit('error', Object.assign(new Error(spec.error), { code: spec.error }));
        if (!spec.hang) child.emit('close', spec.code === undefined ? 0 : spec.code);
    });
    return child;
}

function recorder(plan) {
    const calls = [];
    const spawnImpl = (command, args, options) => {
        calls.push({ command, args, options });
        const spec = plan[command] || { code: 1, stderr: `'${command}' is not recognized` };
        if (typeof spec === 'function') return fakeChild(spec());
        return fakeChild(spec);
    };
    return { calls, spawnImpl };
}

// ---------------------------------------------------------------------------
// Candidate order
// ---------------------------------------------------------------------------

const WIN = {
    backendPath: 'C:\\checkout',
    platform: 'win32',
    envPython: 'C:\\Python312\\python.exe',
    configuredPython: 'D:\\tools\\py312\\python.exe',
    exists: () => true,
};

test('an explicit interpreter outranks the project venv and PATH', () => {
    const candidates = probe.pythonCandidates(WIN);
    assert.deepEqual(candidates.map((c) => c.source), ['env', 'config', 'venv', 'venv', 'venv', 'path']);
    assert.equal(candidates[0].command, 'C:\\Python312\\python.exe');
    assert.equal(candidates[1].command, 'D:\\tools\\py312\\python.exe');
    assert.equal(candidates[candidates.length - 1].command, 'python');
});

test('Windows looks for the Windows venv layout, and creates .venv-win first', () => {
    const candidates = probe.pythonCandidates(WIN);
    const venvs = candidates.filter((c) => c.source === 'venv').map((c) => c.command);
    assert.deepEqual(venvs, [
        path.join('C:\\checkout', '.venv-win', 'Scripts', 'python.exe'),
        path.join('C:\\checkout', '.venv', 'Scripts', 'python.exe'),
        path.join('C:\\checkout', 'venv', 'Scripts', 'python.exe'),
    ]);
    // The POSIX layout is not offered on Windows: a `.venv` created on macOS has
    // no Scripts/ directory, and listing it only made the search look broader.
    assert.equal(venvs.some((v) => v.includes(`bin${path.sep}python`)), false);
});

test('POSIX looks for the POSIX venv layout', () => {
    const candidates = probe.pythonCandidates({
        backendPath: '/checkout', platform: 'darwin', exists: () => true,
    });
    const venvs = candidates.filter((c) => c.source === 'venv').map((c) => c.command);
    assert.deepEqual(venvs, [
        path.join('/checkout', '.venv-win', 'bin', 'python'),
        path.join('/checkout', '.venv', 'bin', 'python'),
        path.join('/checkout', 'venv', 'bin', 'python'),
    ]);
    assert.equal(candidates[candidates.length - 1].command, 'python3');
});

test('a venv that is not on disk is not offered', () => {
    const wanted = path.join('/checkout', '.venv', 'bin', 'python');
    const candidates = probe.pythonCandidates({
        backendPath: '/checkout',
        platform: 'darwin',
        exists: (candidate) => candidate === wanted,
    });
    assert.deepEqual(candidates.map((c) => c.source), ['venv', 'path']);
});

// ---------------------------------------------------------------------------
// Probing
// ---------------------------------------------------------------------------

test('an interpreter that answers is accepted, with its real executable', async () => {
    const { spawnImpl } = recorder({ python3: { stdout: answer([3, 12, 13], '/real/python3') } });
    const result = await probe.probeInterpreter({ command: 'python3', source: 'path' }, { spawnImpl });
    assert.equal(result.ok, true);
    assert.equal(result.version, '3.12.13');
    assert.equal(result.executable, '/real/python3');
});

test('the probe never goes through a shell', async () => {
    const { calls, spawnImpl } = recorder({ python3: { stdout: answer([3, 11, 9]) } });
    await probe.probeInterpreter({ command: 'python3', source: 'path' }, { spawnImpl });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].options.shell, false,
        'a candidate with a space or an ampersand in its path must not be re-parsed by a shell');
    assert.deepEqual(calls[0].args.slice(0, 1), ['-c']);
});

test('the Store placeholder is reported as its own failure', async () => {
    const { spawnImpl } = recorder({
        python: {
            code: 9009,
            stderr: 'Python was not found; run without arguments to install from the Microsoft Store, '
                + 'or disable this shortcut from Settings > Manage App Execution Aliases.',
        },
    });
    const result = await probe.probeInterpreter({ command: 'python', source: 'path' }, { spawnImpl });
    assert.equal(result.ok, false);
    assert.equal(result.failure, 'store_alias');
    assert.match(result.detail, /Microsoft Store/);
});

test('a version outside the supported window is refused, not tolerated', async () => {
    const old = recorder({ python3: { stdout: answer([3, 7, 18]) } });
    const tooOld = await probe.probeInterpreter({ command: 'python3', source: 'path' }, { spawnImpl: old.spawnImpl });
    assert.equal(tooOld.failure, 'unsupported_version');
    assert.match(tooOld.detail, />=3\.8,<3\.14/);

    const new_ = recorder({ python3: { stdout: answer([3, 14, 0]) } });
    const tooNew = await probe.probeInterpreter({ command: 'python3', source: 'path' }, { spawnImpl: new_.spawnImpl });
    assert.equal(tooNew.failure, 'unsupported_version');
});

test('the window endpoints are exactly the build check window', () => {
    assert.deepEqual([...probe.MIN_PYTHON], [3, 8]);
    assert.deepEqual([...probe.MAX_PYTHON_EXCLUSIVE], [3, 14]);
    assert.equal(probe.inWindow([3, 8]), true);
    assert.equal(probe.inWindow([3, 13]), true);
    assert.equal(probe.inWindow([3, 14]), false);
    assert.equal(probe.inWindow([3, 7]), false);
});

test('a candidate that never answers is killed and reported as a timeout', async () => {
    const { calls, spawnImpl } = recorder({ python3: { hang: true } });
    const child = [];
    const recordingSpawn = (command, args, options) => {
        const made = spawnImpl(command, args, options);
        child.push(made);
        return made;
    };
    const result = await probe.probeInterpreter(
        { command: 'python3', source: 'path' },
        { spawnImpl: recordingSpawn, timeoutMs: 30 },
    );
    assert.equal(result.failure, 'timeout');
    assert.equal(child[0].killed, true, 'the hung probe is cleaned up, not left running');
    assert.equal(calls.length, 1);
});

test('a missing executable is reported without throwing', async () => {
    const { spawnImpl } = recorder({ python3: { error: 'ENOENT' } });
    const result = await probe.probeInterpreter({ command: 'python3', source: 'path' }, { spawnImpl });
    assert.equal(result.ok, false);
    assert.equal(result.failure, 'not_found');
});

test('an answer that is not a version is reported as unreadable', async () => {
    const { spawnImpl } = recorder({ python3: { stdout: 'hello from a wrapper\n' } });
    const result = await probe.probeInterpreter({ command: 'python3', source: 'path' }, { spawnImpl });
    assert.equal(result.failure, 'unreadable');
});

test('a chatty wrapper is tolerated as long as the answer is there', async () => {
    const { spawnImpl } = recorder({
        python3: { stdout: 'conda activating...\n' + answer([3, 11, 5]) + '\n' },
    });
    const result = await probe.probeInterpreter({ command: 'python3', source: 'path' }, { spawnImpl });
    assert.equal(result.ok, true);
    assert.equal(result.version, '3.11.5');
});

// ---------------------------------------------------------------------------
// Selection
// ---------------------------------------------------------------------------

test('an explicit interpreter that fails ends the search', async () => {
    const { calls, spawnImpl } = recorder({
        '/broken/python': { code: 1, stderr: 'bad interpreter' },
        '/checkout/.venv/bin/python': { stdout: answer([3, 12, 0]) },
    });
    const selection = await probe.selectInterpreter([
        { command: '/broken/python', source: 'env' },
        { command: '/checkout/.venv/bin/python', source: 'venv' },
    ], { spawnImpl });
    assert.equal(selection.selected, undefined);
    assert.equal(calls.length, 1, 'the other environment is not tried behind the user\'s back');
    assert.equal(selection.attempts[0].failure, 'not_found');
});

test('a broken convenience candidate falls through to the next one', async () => {
    const { calls, spawnImpl } = recorder({
        '/checkout/.venv/bin/python': { code: 9009, stderr: 'Python was not found; Microsoft Store' },
        python3: { stdout: answer([3, 10, 12]) },
    });
    const selection = await probe.selectInterpreter([
        { command: '/checkout/.venv/bin/python', source: 'venv' },
        { command: 'python3', source: 'path' },
    ], { spawnImpl });
    assert.equal(selection.selected.version, '3.10.12');
    assert.equal(calls.length, 2);
    assert.equal(selection.attempts[0].failure, 'store_alias');
});

test('the whole round stops at its budget', async () => {
    const clock = { at: 0 };
    const plan = {
        // This one fails slowly enough to eat the whole round's budget.
        '/slow/python': { code: 9009, stderr: 'Python was not found; Microsoft Store' },
        python3: { stdout: answer([3, 12, 0]) },
    };
    const calls = [];
    const spawnImpl = (command) => {
        calls.push(command);
        clock.at += 20000; // this candidate ate the whole budget
        return fakeChild(plan[command] || { code: 1 });
    };
    const selection = await probe.selectInterpreter([
        { command: '/slow/python', source: 'path' },
        { command: 'python3', source: 'path' },
    ], { spawnImpl, budgetMs: 15000, now: () => clock.at });

    assert.equal(calls.length, 1, 'the second candidate is not started after the budget is gone');
    assert.equal(selection.selected, undefined);
    assert.equal(selection.attempts[1].failure, 'timeout');
    assert.match(selection.attempts[1].detail, /15000 ms/);
});

test('the failure summary names every candidate and its reason', async () => {
    const { spawnImpl } = recorder({
        python: { code: 9009, stderr: 'Microsoft Store' },
    });
    const selection = await probe.selectInterpreter([
        { command: '/checkout/.venv-win/Scripts/python.exe', source: 'venv' },
        { command: 'python', source: 'path' },
    ], { spawnImpl });
    const summary = probe.summariseAttempts(selection.attempts);
    assert.match(summary, /\.venv-win/);
    assert.match(summary, /not_found/);
    assert.match(summary, /python.*store_alias/);
});

// ---------------------------------------------------------------------------
// Against a real interpreter, when this machine has one
// ---------------------------------------------------------------------------

test('a real interpreter on this machine answers the probe', async (t) => {
    const candidates = [
        { command: process.env.COW_TEST_PYTHON, source: 'env' },
        { command: 'python3', source: 'path' },
        { command: 'python', source: 'path' },
    ].filter((candidate) => candidate.command);

    const selection = await probe.selectInterpreter(candidates, { budgetMs: 20000 });
    if (!selection.selected) {
        t.skip(`no usable python on this machine: ${probe.summariseAttempts(selection.attempts)}`);
        return;
    }
    assert.match(selection.selected.version, /^\d+\.\d+\./);
    assert.equal(probe.inWindow(selection.selected.version.split('.').map(Number).slice(0, 2)), true);
    assert.ok(selection.selected.executable, 'sys.executable is reported so a shim is visible');
});
