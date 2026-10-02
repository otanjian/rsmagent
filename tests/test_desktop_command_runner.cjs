// The device-side gate for effectful frames, driven against real processes.
//
// Change ``align-desktop-project-execution-with-master``, tasks 7.1 - 7.6, and
// the fault injections A14 - A20 in ``openspec/.../evidence/faults.md``.
//
// The executor here is not a stub: each "run" spawns a real child process that
// really appends a line to a real file, and the terminator really kills it. That
// is what makes the counts in these tests mean something -- "the script ran
// once" is a line count on disk, not a call counter, so a redelivery that
// slipped past the gate would show up as an extra line rather than as an
// assertion about a mock.
//
// Run: node --test tests/test_desktop_command_runner.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync, spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const srcDir = path.join(desktop, 'src', 'main', 'project-execution');
const distDir = path.join(desktop, 'dist', 'main', 'project-execution');
const samplePath = path.join(root, 'contracts', 'desktop', 'samples', 'v2', 'execute_tool.valid.json');

let gate;

function needsBuild() {
    const compiled = path.join(distDir, 'command-runner.js');
    if (!fs.existsSync(compiled)) return true;
    const newest = Math.max(
        ...fs.readdirSync(srcDir).filter((n) => n.endsWith('.ts'))
            .map((n) => fs.statSync(path.join(srcDir, n)).mtimeMs),
    );
    return newest > fs.statSync(compiled).mtimeMs;
}

before(() => {
    if (needsBuild()) {
        execFileSync('npm', ['run', 'build:main'], { cwd: desktop, stdio: 'inherit' });
    }
    gate = {
        runner: require(path.join(distDir, 'command-runner.js')),
        journal: require(path.join(distDir, 'journal.js')),
    };
});

// A fixed clock: the sample frame expires at 2026-10-01T00:10:00Z, and the
// contract's expiry rule is one of the things under test, so "now" must not
// drift with the wall clock.
const NOW = Date.parse('2026-09-30T21:00:00Z');

const IDENTITY = {
    origin: 'https://master.example',
    user_id: 'u-1',
    tenant_id: 't-1',
    device_id: 'dev-1',
};

/** The real work one run does: one line appended to a real file. */
const APPEND_SCRIPT = [
    "const fs = require('fs');",
    "fs.appendFileSync(process.env.COW_LOG, process.env.COW_CMD + '\\n');",
    "setTimeout(() => process.exit(0), Number(process.env.COW_DELAY_MS || 0));",
].join('\n');

function scratch() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'command-runner-'));
    return { dir, log: path.join(dir, 'effects.log'), journalFile: path.join(dir, 'journal.json') };
}

function frame(overrides = {}) {
    const base = JSON.parse(fs.readFileSync(samplePath, 'utf8'));
    return { ...base, ...overrides };
}

/**
 * Real executor: a child process per run, a real file effect, a real kill.
 *
 * ``delayMs`` keeps a run alive long enough to be cancelled; ``roots`` maps a
 * workspace alias to the root the runner resolved for it, so the stats can be
 * attributed per root.
 */
function harness(log) {
    const children = new Map();
    const started = [];
    let active = 0;
    let maxActive = 0;

    const execute = (request) => new Promise((resolve) => {
        const id = String(request.frame.command_id);
        active += 1;
        maxActive = Math.max(maxActive, active);
        started.push(id);
        const child = spawn(process.execPath, ['-e', APPEND_SCRIPT], {
            env: {
                ...process.env,
                COW_LOG: log,
                COW_CMD: id,
                COW_DELAY_MS: String(Number(request.frame.sleep_ms || 0)),
            },
            stdio: 'ignore',
        });
        children.set(id, child);
        child.on('exit', (code, signal) => {
            children.delete(id);
            active -= 1;
            resolve({ exit_code: signal ? -1 : (code ?? 0) });
        });
    });

    const terminate = async (handle) => {
        const child = children.get(handle.commandId);
        if (!child) return;
        const ended = new Promise((resolve) => child.on('exit', resolve));
        child.kill('SIGKILL');
        await ended;
    };

    return {
        execute,
        terminate,
        started,
        maxActive: () => maxActive,
        running: () => [...children.keys()],
    };
}

function build({ dir, log, journalFile, roots, now = NOW, onReconcile, connected, pathsOf }) {
    void dir;
    const h = harness(log);
    const journal = new gate.journal.JournalStore({ file: journalFile, now: () => now });
    const runner = new gate.runner.CommandRunner({
        journal,
        // Two workspace aliases can resolve to the same real root; that is the
        // case the serialisation key exists for.
        realRootOf: (workspaceId) => roots[workspaceId] ?? null,
        execute: h.execute,
        terminate: h.terminate,
        now: () => now,
        connected,
        onReconcile,
        pathsOf,
        newJournalId: (() => { let n = 0; return () => `journal_${++n}`; })(),
    });
    return { runner, journal, ...h, dir };
}

function lines(log) {
    if (!fs.existsSync(log)) return 0;
    return fs.readFileSync(log, 'utf8').split('\n').filter(Boolean).length;
}

function commandIds(log) {
    if (!fs.existsSync(log)) return [];
    return fs.readFileSync(log, 'utf8').split('\n').filter(Boolean).sort();
}

async function waitForLines(log, count, timeoutMs = 4000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (lines(log) >= count) return true;
        await new Promise((r) => setTimeout(r, 10));
    }
    return lines(log) >= count;
}

// -- A14: a redelivery must not run the effect twice ----------------------

test('a frame is executed once and leaves a real receipt', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });

    const result = await h.runner.run(frame(), IDENTITY);

    assert.equal(result.outcome, 'ran');
    assert.equal(result.phase, 'succeeded');
    assert.equal(result.effects, 'completed');
    assert.deepEqual(commandIds(s.log), ['cmd_example']);
    assert.equal(result.entry.state, 'completed');
    assert.equal(result.entry.receipt.outcome, 'succeeded');
});

test('A14: a redelivery before the receipt is durable returns the stored result and does not re-run', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const f = frame();

    const first = await h.runner.run(f, IDENTITY);
    // The device lost the network before the receipt reached the server, so the
    // server re-sends the same command.
    const second = await h.runner.run(f, IDENTITY);

    assert.equal(second.outcome, 'replayed');
    assert.equal(second.entry.journal_id, first.entry.journal_id);
    assert.equal(second.effects, 'completed');
    assert.deepEqual(commandIds(s.log), ['cmd_example'], 'the script must not run a second time');
    assert.deepEqual(h.started, ['cmd_example']);
});

test('A14: a second delivery while the first is still running is not started alongside it', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const f = frame({ sleep_ms: 150 });

    const first = h.runner.run(f, IDENTITY);
    assert.ok(await waitForLines(s.log, 1));
    const second = await h.runner.run(f, IDENTITY);
    await first;

    assert.equal(second.outcome, 'unresolved');
    assert.equal(second.phase, 'outcome_unknown');
    assert.deepEqual(h.started, ['cmd_example'], 'the in-flight run owns the command id');
    assert.deepEqual(commandIds(s.log), ['cmd_example']);
});

// -- A16: tampering, expiry, and cleaned-up receipts ----------------------

test('A16: the same command id with different arguments is refused, and the first payload is untouched', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const original = frame();
    await h.runner.run(original, IDENTITY);

    const tampered = frame({
        params_digest: 'sha256:2222222222222222222222222222222222222222222222222222222222222222',
        // Deliberately also invalid, so "refused as a conflict" cannot be
        // confused with "refused for being a bad frame": a conflict is reported
        // as a conflict, from the record, before the payload is even judged.
        tool: 'not_a_real_tool',
        arguments: { command: 'rm -rf /' },
    });
    const result = await h.runner.run(tampered, IDENTITY);

    assert.equal(result.outcome, 'refused');
    assert.equal(result.code, 'command_conflict');
    assert.equal(result.effects, 'none');
    assert.deepEqual(commandIds(s.log), ['cmd_example']);
    assert.equal(h.runner.admit(tampered, IDENTITY).kind, 'refused');
});

test('A16: a frame past its deadline is expired, not started late', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const stale = frame({ command_id: 'cmd_late', expires_at: '2026-09-30T20:59:59Z' });

    const result = await h.runner.run(stale, IDENTITY);

    assert.equal(result.outcome, 'refused');
    assert.equal(result.code, 'expired');
    assert.equal(result.phase, 'expired');
    assert.equal(result.effects, 'none');
    assert.equal(lines(s.log), 0, 'a late frame never touches the project');
});

test('A16: a command whose receipt was cleaned up cannot become executable again', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const f = frame({ expires_at: '2027-01-01T00:00:00Z' });
    await h.runner.run(f, IDENTITY);

    // Eight days later the receipt is pruned, leaving only a tombstone.
    const later = NOW + 8 * 24 * 60 * 60 * 1000;
    const runner = new gate.runner.CommandRunner({
        journal: new gate.journal.JournalStore({ file: s.journalFile, now: () => later }),
        realRootOf: (workspaceId) => (workspaceId === 'ws_example' ? '/proj/a' : null),
        execute: h.execute,
        terminate: h.terminate,
        now: () => later,
    });
    assert.deepEqual(runner.sweepRetention(), ['journal_1']);
    const journal = JSON.parse(fs.readFileSync(s.journalFile, 'utf8'));
    assert.equal(journal.entries.length, 0, 'the receipt is gone');
    assert.equal(journal.pruned.length, 1, 'but the key is remembered');

    const again = await runner.run(f, IDENTITY);
    assert.equal(again.outcome, 'refused');
    assert.equal(again.code, 'command_conflict');
    assert.deepEqual(commandIds(s.log), ['cmd_example']);
});

// -- A15: crash between the intent and the receipt ------------------------

test('A15: a crash after the start intent leaves outcome_unknown, never a silent re-run', async () => {
    const s = scratch();
    const roots = { ws_example: '/proj/a' };
    const f = frame({ command_id: 'cmd_crash' });

    // The first process wrote its intent and then died before running anything.
    const dead = new gate.journal.JournalStore({ file: s.journalFile, now: () => NOW });
    dead.begin({
        journal_id: 'journal_died',
        command_id: 'cmd_crash',
        params_digest: f.params_digest,
        origin: IDENTITY.origin,
        user_id: IDENTITY.user_id,
        tenant_id: IDENTITY.tenant_id,
        device_id: IDENTITY.device_id,
        workspace_id: 'ws_example',
        run_id: f.run_id,
        tool_call_id: f.tool_call_id,
        tool: f.tool,
        grant_version: f.grant_version,
        started_at: NOW,
    }, {
        origin: IDENTITY.origin, user_id: IDENTITY.user_id, tenant_id: IDENTITY.tenant_id,
        device_id: IDENTITY.device_id, command_id: 'cmd_crash',
    });

    const reconciled = [];
    const h = build({ ...s, roots, onReconcile: (entry, reason) => reconciled.push([entry.command_id, reason]) });
    const unresolved = h.runner.recoverUnfinished();

    assert.deepEqual(unresolved.map((e) => e.command_id), ['cmd_crash']);
    assert.equal(reconciled.length, 1);
    assert.match(reconciled[0][1], /inspect|stopped/);

    const result = await h.runner.run(f, IDENTITY);
    assert.equal(result.outcome, 'unresolved');
    assert.equal(result.phase, 'outcome_unknown');
    assert.equal(result.effects, 'unknown');
    assert.equal(lines(s.log), 0, 'an unknown outcome is never resolved by running it again');
    assert.deepEqual(h.started, []);
});

// -- A20 / task 7.4: serialisation keyed on the real root -----------------

test('A20: two aliases of one real root serialise effectful commands', async () => {
    const s = scratch();
    const h = build({
        ...s,
        // Two workspaces, one directory: an alias must not get its own gate.
        roots: { ws_alias_a: '/proj/shared', ws_alias_b: '/proj/shared' },
    });
    const first = frame({ command_id: 'cmd_one', workspace_id: 'ws_alias_a', sleep_ms: 120 });
    const second = frame({
        command_id: 'cmd_two', workspace_id: 'ws_alias_b', run_id: 'run_2',
        tool_call_id: 'call_2', sleep_ms: 0,
    });

    await Promise.all([h.runner.run(first, IDENTITY), h.runner.run(second, IDENTITY)]);

    assert.equal(h.maxActive(), 1, 'effectful_per_project = 1 is keyed on the real root');
    assert.deepEqual(commandIds(s.log), ['cmd_one', 'cmd_two']);
});

test('A20: different real roots are not serialised against each other', async () => {
    const s = scratch();
    const h = build({
        ...s, roots: { ws_a: '/proj/a', ws_b: '/proj/b' },
    });
    const first = frame({ command_id: 'cmd_a', workspace_id: 'ws_a', sleep_ms: 120 });
    const second = frame({ command_id: 'cmd_b', workspace_id: 'ws_b', sleep_ms: 120 });

    await Promise.all([h.runner.run(first, IDENTITY), h.runner.run(second, IDENTITY)]);

    assert.equal(h.maxActive(), 2, 'a different project is a different queue');
});

test('task 7.4: read-only commands keep the contract parallelism on one root', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const reads = [1, 2, 3, 4, 5].map((n) => frame({
        command_id: `cmd_read_${n}`, run_id: `run_${n}`, tool_call_id: `call_${n}`,
        tool: 'read', arguments: { path: 'file.txt' }, sleep_ms: 80,
    }));

    await Promise.all(reads.map((f) => h.runner.run(f, IDENTITY)));

    assert.equal(h.maxActive(), 4, 'readonly_parallel_per_project = 4, not unbounded');
    assert.equal(lines(s.log), 5);
});

// -- A18: cancellation is a real process-tree termination ------------------

test('A18: a cancel resolves only after the process ended, and keeps the files already written', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const f = frame({ command_id: 'cmd_long', sleep_ms: 30_000 });

    const running = h.runner.run(f, IDENTITY);
    assert.ok(await waitForLines(s.log, 1), 'the script must have written before the cancel');
    assert.deepEqual(h.running(), ['cmd_long']);

    const scope = {
        origin: IDENTITY.origin, user_id: IDENTITY.user_id, tenant_id: IDENTITY.tenant_id,
        device_id: IDENTITY.device_id, command_id: 'cmd_long',
    };
    const cancelled = await h.runner.cancel('cmd_long', scope, f.params_digest);
    // Checked before awaiting the run: "cancel returned" must already mean "the
    // tree is gone" and "the receipt is durable", not "a kill was requested".
    assert.equal(cancelled, true);
    assert.deepEqual(h.running(), [], 'the process tree is gone when cancel resolves');
    assert.equal(fs.readFileSync(s.journalFile, 'utf8').includes('"outcome":"cancelled"'), true,
        'the verdict is recorded before cancel returns');
    const result = await running;

    assert.equal(result.outcome, 'cancelled');
    assert.equal(result.phase, 'cancelled');
    assert.equal(result.effects, 'unknown', 'a stopped process never promises a rollback');
    assert.equal(lines(s.log), 1, 'what it wrote before stopping is kept');
});

test('A18: a late result from a cancelled run cannot overwrite the cancellation', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const f = frame({ command_id: 'cmd_race', sleep_ms: 30_000 });

    const running = h.runner.run(f, IDENTITY);
    assert.ok(await waitForLines(s.log, 1));
    const scope = {
        origin: IDENTITY.origin, user_id: IDENTITY.user_id, tenant_id: IDENTITY.tenant_id,
        device_id: IDENTITY.device_id, command_id: 'cmd_race',
    };
    // Cancel first: the receipt is written while the executor is still settling.
    await h.runner.cancel('cmd_race', scope, f.params_digest);
    const result = await running;

    assert.equal(result.phase, 'cancelled');
    const journal = JSON.parse(fs.readFileSync(s.journalFile, 'utf8'));
    assert.equal(journal.entries[0].receipt.outcome, 'cancelled');
});

test('A18: cancelling a queued command writes effects none and never runs it', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const holder = frame({ command_id: 'cmd_holder', sleep_ms: 150 });
    const queued = frame({
        command_id: 'cmd_queued', run_id: 'run_q', tool_call_id: 'call_q',
    });

    const first = h.runner.run(holder, IDENTITY);
    const second = h.runner.run(queued, IDENTITY);
    const scope = {
        origin: IDENTITY.origin, user_id: IDENTITY.user_id, tenant_id: IDENTITY.tenant_id,
        device_id: IDENTITY.device_id, command_id: 'cmd_queued',
    };

    assert.equal(await h.runner.cancel('cmd_queued', scope, queued.params_digest), true);
    const [a, b] = await Promise.all([first, second]);

    assert.equal(a.phase, 'succeeded');
    assert.equal(b.phase, 'cancelled');
    assert.equal(b.effects, 'none', 'the queue never reached the worker');
    assert.deepEqual(commandIds(s.log), ['cmd_holder']);
});

test('A18: cancelling something this device is not running is refused, not invented', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const scope = {
        origin: IDENTITY.origin, user_id: IDENTITY.user_id, tenant_id: IDENTITY.tenant_id,
        device_id: IDENTITY.device_id, command_id: 'cmd_absent',
    };
    assert.equal(await h.runner.cancel('cmd_absent', scope, 'sha256:aa'), false);
});

// -- honesty about failure and about the journal ---------------------------

test('a run that dies mid-way records outcome_unknown, not "nothing happened"', async () => {
    const s = scratch();
    const roots = { ws_example: '/proj/a' };
    const h = build({ ...s, roots });
    const runner = new gate.runner.CommandRunner({
        journal: new gate.journal.JournalStore({ file: s.journalFile, now: () => NOW }),
        realRootOf: (id) => roots[id] ?? null,
        execute: async () => { throw new Error('the worker exited mid-write'); },
        terminate: h.terminate,
        now: () => NOW,
    });

    const result = await runner.run(frame({ command_id: 'cmd_died' }), IDENTITY);

    assert.equal(result.outcome, 'ran');
    assert.equal(result.phase, 'outcome_unknown');
    assert.equal(result.effects, 'unknown');
    assert.equal(result.code, 'tool_failed');
});

test('an unbound workspace is refused before anything is recorded or run', async () => {
    const s = scratch();
    const h = build({ ...s, roots: {} });
    const result = await h.runner.run(frame(), IDENTITY);

    assert.equal(result.code, 'stale_context');
    assert.equal(result.effects, 'none');
    assert.deepEqual(h.started, []);
    assert.equal(fs.existsSync(s.journalFile), false, 'a refused frame leaves no journal');
});

test('an unreadable journal refuses effectful work and quarantines the file', async () => {
    const s = scratch();
    fs.writeFileSync(s.journalFile, 'not json at all');
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });

    const result = await h.runner.run(frame(), IDENTITY);

    assert.equal(result.code, 'journal_unavailable');
    assert.equal(result.effects, 'none');
    assert.deepEqual(h.started, []);
    assert.ok(fs.existsSync(`${s.journalFile}.corrupt`));
});

test('a frame that smuggles a cwd or a module is refused before the journal', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });

    const withCwd = await h.runner.run(frame({ cwd: '/etc' }), IDENTITY);
    const withModule = await h.runner.run(frame({
        command_id: 'cmd_mod', arguments: { command: 'x', module: 'os' },
    }), IDENTITY);

    assert.equal(withCwd.code, 'invalid_request');
    assert.equal(withModule.code, 'invalid_request');
    assert.equal(lines(s.log), 0);
    assert.equal(fs.existsSync(s.journalFile), false, 'a refused frame leaves no record');
});

test('an unsupported platform is refused by name rather than translated', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const result = await h.runner.run(frame({ platform: 'win32' }), IDENTITY);
    assert.equal(result.code, 'invalid_request');
    assert.match(result.message, /unsupported on platform 'win32'/);
});

// -- A19: short and long outages ------------------------------------------

test('A19: while the connection is down no new frame is accepted', async () => {
    const s = scratch();
    let online = true;
    const h = build({ ...s, roots: { ws_example: '/proj/a' }, connected: () => online });

    online = false;
    const refused = await h.runner.run(frame(), IDENTITY);
    assert.equal(refused.code, 'device_offline');
    assert.equal(refused.effects, 'none');
    assert.deepEqual(h.started, []);
    assert.equal(h.runner.accepts(IDENTITY), false);

    // Back online: a *new* command is accepted, and the journal -- not a retry --
    // is what makes the old one impossible to run twice.
    online = true;
    const accepted = await h.runner.run(frame({ command_id: 'cmd_after' }), IDENTITY);
    assert.equal(accepted.outcome, 'ran');
    assert.deepEqual(commandIds(s.log), ['cmd_after']);
});

test('A19: an outage inside the liveness window terminates nothing', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const f = frame({ command_id: 'cmd_short_gap', sleep_ms: 30_000 });

    const running = h.runner.run(f, IDENTITY);
    assert.ok(await waitForLines(s.log, 1));
    const abandoned = await h.runner.abandonStaleRuns({ disconnectedForMs: 1_000 });
    assert.deepEqual(abandoned, [], 'a short gap is the reconnect\'s job, not the reaper\'s');
    assert.deepEqual(h.running(), ['cmd_short_gap']);

    const scope = {
        origin: IDENTITY.origin, user_id: IDENTITY.user_id, tenant_id: IDENTITY.tenant_id,
        device_id: IDENTITY.device_id, command_id: 'cmd_short_gap',
    };
    await h.runner.cancel('cmd_short_gap', scope, f.params_digest);
    await running;
});

test('A19: the exact liveness boundary is still inside the window', async () => {
    // The rule is "past the window" (see abandonStaleRuns), so exactly at the
    // window the run must be left alone. Without this case `<=` and `<` are
    // indistinguishable, and the stricter reading stops a run that a reconnect
    // could still have reconciled.
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const f = frame({ command_id: 'cmd_at_boundary', sleep_ms: 30_000 });
    const livenessMs = 30_000;

    const running = h.runner.run(f, IDENTITY);
    assert.ok(await waitForLines(s.log, 1));

    const abandoned = await h.runner.abandonStaleRuns({ disconnectedForMs: livenessMs });
    assert.deepEqual(abandoned, [], 'exactly at the window is not yet past it');
    assert.deepEqual(h.running(), ['cmd_at_boundary']);

    const scope = {
        origin: IDENTITY.origin, user_id: IDENTITY.user_id, tenant_id: IDENTITY.tenant_id,
        device_id: IDENTITY.device_id, command_id: 'cmd_at_boundary',
    };
    await h.runner.cancel('cmd_at_boundary', scope, f.params_digest);
    await running;
});

test('A19: past the liveness window the run is stopped and what it wrote is kept', async () => {
    const s = scratch();
    const reconciled = [];
    const h = build({
        ...s, roots: { ws_example: '/proj/a' },
        onReconcile: (entry, reason) => reconciled.push([entry.command_id, reason]),
    });
    const f = frame({ command_id: 'cmd_lost', sleep_ms: 30_000 });
    const livenessMs = 30_000;

    const running = h.runner.run(f, IDENTITY);
    assert.ok(await waitForLines(s.log, 1));

    const abandoned = await h.runner.abandonStaleRuns({ disconnectedForMs: livenessMs + 1 });

    assert.deepEqual(abandoned.map((e) => e.command_id), ['cmd_lost']);
    assert.deepEqual(h.running(), [], 'the tree is really gone, not just abandoned in name');
    assert.equal(abandoned[0].receipt.outcome, 'cancelled');
    assert.equal(abandoned[0].receipt.effects, 'unknown',
        'the connection was lost, so the device cannot claim nothing was written');
    assert.equal(lines(s.log), 1, 'partial output is kept for the user to inspect');
    assert.equal(reconciled.length, 1);
    assert.match(reconciled[0][1], /liveness window/);

    const result = await running;
    assert.equal(result.phase, 'cancelled');
    // And the same frame cannot be replayed later as a fresh run.
    const again = await h.runner.run(f, IDENTITY);
    assert.equal(again.outcome, 'replayed');
    assert.deepEqual(commandIds(s.log), ['cmd_lost'], 'never re-run automatically');
});

// -- A18: sign-out and tenant switch --------------------------------------

test('A18: a sign-out stops this session\'s running work and refuses its frames afterwards', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    const f = frame({ command_id: 'cmd_signedout', sleep_ms: 30_000 });

    const running = h.runner.run(f, IDENTITY);
    assert.ok(await waitForLines(s.log, 1));

    const stopped = await h.runner.revokeIdentity(IDENTITY);

    assert.equal(stopped, 1);
    assert.deepEqual(h.running(), [], 'the process tree ends before the session does');
    assert.equal(h.runner.accepts(IDENTITY), false);
    const result = await running;
    assert.equal(result.phase, 'cancelled');
    assert.equal(result.effects, 'unknown', 'a stopped run is not a rolled-back one');
    assert.equal(lines(s.log), 1);

    const refused = await h.runner.run(frame({ command_id: 'cmd_new' }), IDENTITY);
    assert.equal(refused.code, 'grant_revoked');
    assert.deepEqual(commandIds(s.log), ['cmd_signedout']);
});

test('A18: a tenant switch stops only the session that is going away', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a', ws_other: '/proj/b' } });
    const other = { ...IDENTITY, tenant_id: 't-2', device_id: 'dev-2' };
    const mine = frame({ command_id: 'cmd_mine', sleep_ms: 30_000 });
    const theirs = frame({
        command_id: 'cmd_theirs', workspace_id: 'ws_other', run_id: 'run_o',
        tool_call_id: 'call_o', sleep_ms: 30_000,
    });

    const a = h.runner.run(mine, IDENTITY);
    const b = h.runner.run(theirs, other);
    assert.ok(await waitForLines(s.log, 2));

    assert.equal(await h.runner.revokeIdentity({ ...IDENTITY, tenant_id: 't-3' }), 0);
    assert.equal(await h.runner.revokeIdentity(IDENTITY), 1);

    assert.deepEqual(h.running(), ['cmd_theirs'], 'the other tenant keeps its run');
    assert.equal(h.runner.accepts(IDENTITY), false);
    assert.equal(h.runner.accepts(other), true);
    assert.equal((await a).phase, 'cancelled');
    await h.runner.cancel('cmd_theirs', {
        origin: other.origin, user_id: other.user_id, tenant_id: other.tenant_id,
        device_id: other.device_id, command_id: 'cmd_theirs',
    }, theirs.params_digest);
    assert.equal((await b).phase, 'cancelled');
});

// -- 7.6: retention and the record cap ------------------------------------

test('sweepRetention ages out receipts and is bounded by keepAtMost without touching unknowns', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    for (let n = 1; n <= 6; n += 1) {
        await h.runner.run(frame({
            command_id: `cmd_${n}`, run_id: `run_${n}`, tool_call_id: `call_${n}`,
        }), IDENTITY);
    }
    // One intent that never got a receipt. Its evidence must survive every
    // sweep: it is the only record that the command might have had an effect.
    const pending = frame({ command_id: 'cmd_unknown', run_id: 'run_u', tool_call_id: 'call_u' });
    h.journal.begin({
        journal_id: 'journal_u', command_id: 'cmd_unknown', params_digest: pending.params_digest,
        origin: IDENTITY.origin, user_id: IDENTITY.user_id, tenant_id: IDENTITY.tenant_id,
        device_id: IDENTITY.device_id, workspace_id: 'ws_example', run_id: pending.run_id,
        tool_call_id: pending.tool_call_id, tool: pending.tool, grant_version: 3, started_at: NOW,
    }, {
        origin: IDENTITY.origin, user_id: IDENTITY.user_id, tenant_id: IDENTITY.tenant_id,
        device_id: IDENTITY.device_id, command_id: 'cmd_unknown',
    });
    assert.equal(h.runner.recoverUnfinished().length, 1);

    const removed = h.runner.sweepRetention({ keepAtMost: 2 });
    const journal = JSON.parse(fs.readFileSync(s.journalFile, 'utf8'));

    // Seven records, a cap of two: the unknown occupies one of the two slots,
    // so five finished receipts go -- the oldest first.
    assert.equal(removed.length, 5, 'the cap trimmed the oldest finished receipts');
    assert.deepEqual(journal.entries.map((e) => e.command_id).sort(), ['cmd_6', 'cmd_unknown'],
        'the unknown survives both the age window and the cap');
    assert.ok(journal.pruned.length >= 5, 'each reclaimed receipt leaves a tombstone');
});

test('the retention sweep uses the contract window when none is given', async () => {
    const s = scratch();
    const h = build({ ...s, roots: { ws_example: '/proj/a' } });
    await h.runner.run(frame({ command_id: 'cmd_fresh' }), IDENTITY);
    assert.deepEqual(h.runner.sweepRetention(), [], 'a fresh receipt is inside the window');
});

// -- A20 / task 7.4: an outside edit is reported, not swallowed ------------

test('A20: an edit reports an outside change from the version this device served', async () => {
    const s = scratch();
    const target = path.join(s.dir, 'note.txt');
    fs.writeFileSync(target, 'first\n');
    // The model read the file; then someone else rewrote it before the edit.
    const h = build({
        ...s,
        roots: { ws_example: '/proj/a' },
        pathsOf: () => ({ modifies: [target] }),
    });
    h.runner.versions.noteServed(target);
    fs.writeFileSync(target, 'someone else changed this\n');

    const result = await h.runner.run(frame(), IDENTITY);

    // The edit is *applied* -- the contract's ``file_changed`` is ``partial`` --
    // and the warning names the path and the reason.
    assert.equal(result.outcome, 'ran');
    assert.equal(result.code, 'file_changed');
    assert.match(result.message, /note\.txt was modified after you last read it/);
    assert.equal(lines(s.log), 1, 'the warning does not block the effect');
});

test('A20: a file deleted after the read is reported as deleted', async () => {
    const s = scratch();
    const target = path.join(s.dir, 'gone.txt');
    fs.writeFileSync(target, 'here\n');
    const h = build({
        ...s,
        roots: { ws_example: '/proj/a' },
        pathsOf: () => ({ modifies: [target] }),
    });
    h.runner.versions.noteServed(target);
    fs.unlinkSync(target);

    const result = await h.runner.run(frame(), IDENTITY);

    assert.equal(result.code, 'file_changed');
    assert.match(result.message, /gone\.txt was deleted after you last read it/);
});

test('A20: a first write to a file never read is not stale', async () => {
    const s = scratch();
    const target = path.join(s.dir, 'brand-new.txt');
    const h = build({
        ...s,
        roots: { ws_example: '/proj/a' },
        pathsOf: () => ({ modifies: [target] }),
    });

    const result = await h.runner.run(frame(), IDENTITY);

    assert.ok(!result.code, 'no expectation means no warning');
    assert.equal(lines(s.log), 1);
});

test('A20: our own successful write makes the next edit current again', async () => {
    const s = scratch();
    const target = path.join(s.dir, 'ours.txt');
    fs.writeFileSync(target, 'v1\n');
    const h = build({
        ...s,
        roots: { ws_example: '/proj/a' },
        pathsOf: () => ({ modifies: [target] }),
    });
    h.runner.versions.noteServed(target);
    // The tool itself rewrites the file; that is the effect this run performs.
    fs.writeFileSync(target, 'v2\n');

    const first = await h.runner.run(frame(), IDENTITY);
    assert.equal(first.code, 'file_changed', 'the run that raced the edit is warned');

    const second = await h.runner.run(frame({
        command_id: 'cmd_two', run_id: 'run_2', tool_call_id: 'call_2',
    }), IDENTITY);
    assert.ok(!second.code, 'our write re-established the view');
    assert.equal(lines(s.log), 2);
});
