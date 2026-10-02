// The device end of the v2 execution channel, driven against real processes.
//
// Change ``align-desktop-project-execution-with-master``, tasks 7.1 - 7.8, and
// the fault injections A14 - A20 in ``openspec/.../evidence/faults.md``.
//
// Same rule as ``test_desktop_command_runner.cjs``: the executor is not a stub.
// Each admitted frame spawns a real child process that really appends a line to
// a real file, so "it ran once" is a line count on disk rather than a call
// counter -- a redelivery that slipped past the journal would show up as an
// extra line.
//
// Run: node --test tests/test_desktop_device_execution.cjs

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

let api;

function needsBuild() {
    const compiled = path.join(distDir, 'device-execution.js');
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
    api = {
        execution: require(path.join(distDir, 'device-execution.js')),
        journal: require(path.join(distDir, 'journal.js')),
        contract: require(path.join(distDir, 'contract.js')),
        cache: require(path.join(distDir, 'skill-cache.js')),
        pkg: require(path.join(distDir, 'skill-package.js')),
    };
});

// The sample frame expires at 2026-10-01T00:10:00Z, and the contract's expiry
// rule is under test, so "now" must not drift with the wall clock.
const NOW = Date.parse('2026-09-30T21:00:00Z');

// The sample names ``dev_example``; the device must agree with the frame.
const IDENTITY = {
    origin: 'https://master.example',
    user_id: 'u-1',
    tenant_id: 't-1',
    device_id: 'dev_example',
};

/** The real work one run does: one line appended to a real file. */
const APPEND_SCRIPT = [
    "const fs = require('fs');",
    "fs.appendFileSync(process.env.COW_LOG, process.env.COW_CMD + '\\n');",
    "setTimeout(() => process.exit(0), Number(process.env.COW_DELAY_MS || 0));",
].join('\n');

function scratch() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'device-exec-'));
    return {
        dir,
        log: path.join(dir, 'effects.log'),
        project: path.join(dir, 'project'),
        journalFile: path.join(dir, 'journal.json'),
    };
}

function frame(overrides = {}) {
    const base = JSON.parse(fs.readFileSync(samplePath, 'utf8'));
    const merged = { ...base, ...overrides };
    if (!('skill_resources' in overrides)) {
        // The contract sample declares one skill version with an all-zero
        // placeholder digest. The device gate is real, so the default here is the
        // version a device would really hold: the placeholder is replaced with the
        // digest the package actually hashes to, and ``build`` seeds a cache with
        // it. A frame declaring a version nobody holds is the refusal the skill
        // tests below cover deliberately, not a state every other test should
        // stumble into.
        merged.skill_resources = [
            { skill_id: SAMPLE_SKILL_ID, digest: sampleSkill().digest },
        ];
    }
    return merged;
}

/** Real executor: a child process per run, a real file effect, a real kill. */
function harness(log) {
    const children = new Map();
    const started = [];
    let active = 0;
    let maxActive = 0;

    const runTool = (request) => new Promise((resolve) => {
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
            resolve({ exit_code: signal ? -1 : (code ?? 0), stdout: `ran ${id}` });
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
        runTool,
        terminate,
        started,
        maxActive: () => maxActive,
        running: () => [...children.keys()],
    };
}

function build({
    log, project, journalFile, roots, now = NOW, onReconcile, connected,
    identity = IDENTITY, platform, newJournalId, skillCache, fetchSkills,
}) {
    const h = harness(log);
    // ``undefined`` means "the ordinary device: it holds the version the frame
    // declares"; ``null`` means "a device with no skill cache at all", which is
    // the refusal one of the tests is about.
    const resolvedCache = skillCache === undefined ? defaultSkillCache() : skillCache;
    const execution = new api.execution.DeviceExecution({
        identity: () => (typeof identity === 'function' ? identity() : identity),
        realRootOf: (workspaceId) => roots[workspaceId] ?? null,
        runTool: h.runTool,
        terminate: h.terminate,
        journalFile,
        now: () => now,
        connected,
        onReconcile,
        platform: platform ?? 'darwin',
        newJournalId: newJournalId ?? (() => { let n = 0; return () => `journal_${++n}`; })(),
        ...(resolvedCache !== undefined ? { skillCache: resolvedCache } : {}),
        ...(fetchSkills !== undefined ? { fetchSkills } : {}),
    });
    return { execution, ...h, project };
}

// -- skill versions (task 8.9) --------------------------------------------

/** The skill id the contract's own ``execute_tool`` sample declares. */
const SAMPLE_SKILL_ID = 'example-xlsx';

/** A skill version the tests install themselves, distinct from the sample's. */
const OTHER_SKILL_ID = 'builtin:summary-workbook';

/** One version's files, and the digest the package hashes to. */
function skillBundle(overrides) {
    const files = {
        'SKILL.md': Buffer.from('---\nname: summary-workbook\ndescription: d\n---\n', 'utf-8'),
        'scripts/summarize.py': Buffer.from("print('summarize')\n", 'utf-8'),
        ...(overrides || {}),
    };
    return Object.entries(files).map(([relativePath, body]) => ({ relativePath, body }));
}

function skillCache() {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'device-skill-'));
    return { cache: new api.cache.SkillCache(root), root };
}

/** The sample's declared version, as real files with a real digest. */
function sampleSkill() {
    const entries = skillBundle({
        'SKILL.md': Buffer.from('---\nname: example-xlsx\ndescription: d\n---\n', 'utf-8'),
    });
    return { entries, digest: api.pkg.packageDigest(entries) };
}

/** A cache that already holds the sample's declared version. */
function defaultSkillCache() {
    const { cache } = skillCache();
    const { entries, digest } = sampleSkill();
    cache.install(SAMPLE_SKILL_ID, digest, entries);
    return cache;
}

test('a frame whose skills are not here is refused before anything runs', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const { cache } = skillCache();
    const digest = api.pkg.packageDigest(skillBundle());
    const h = build({ ...s, roots: { ws_example: s.project }, skillCache: cache });

    const reply = await h.execution.execute(frame({
        skill_resources: [{ skill_id: OTHER_SKILL_ID, digest }],
    }));

    assert.equal(reply.state, 'failed');
    assert.equal(reply.error_code, 'incompatible_skill');
    assert.equal(reply.effects, 'none');
    // Not run: a device that cannot prove which version it would run must not run
    // one, so the worker was never asked to start.
    assert.deepEqual(h.started, []);
    assert.equal(lines(s.log), 0);
});

test('a frame declaring skills with no cache at all is refused, not run', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const digest = api.pkg.packageDigest(skillBundle());
    const h = build({ ...s, roots: { ws_example: s.project }, skillCache: null });

    const reply = await h.execution.execute(frame({
        skill_resources: [{ skill_id: OTHER_SKILL_ID, digest }],
    }));

    assert.equal(reply.error_code, 'skill_cache_unavailable');
    assert.deepEqual(h.started, []);
});

test('a version obtained by the fetch hook lets the run proceed', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const { cache } = skillCache();
    const entries = skillBundle();
    const digest = api.pkg.packageDigest(entries);
    const seen = [];
    const h = build({
        ...s, roots: { ws_example: s.project }, skillCache: cache,
        fetchSkills: async (frame2, declared) => {
            seen.push(declared);
            for (const entry of declared) cache.install(entry.skillId, entry.digest, entries);
        },
    });

    const reply = await h.execution.execute(frame({
        skill_resources: [{ skill_id: OTHER_SKILL_ID, digest }],
    }));

    assert.equal(reply.state, 'succeeded');
    assert.equal(lines(s.log), 1);
    assert.deepEqual(seen, [[{ skillId: OTHER_SKILL_ID, digest }]]);
    // The cache is the authority, and the roots it resolves are what the worker
    // is granted read-only.
    assert.deepEqual(h.execution.skillRootsFor(
        frame({ skill_resources: [{ skill_id: OTHER_SKILL_ID, digest }] })),
        [cache.versionDir(OTHER_SKILL_ID, digest)]);
});

test('a fetch that cannot deliver refuses the run with its own code', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const { cache } = skillCache();
    const digest = api.pkg.packageDigest(skillBundle());
    const h = build({
        ...s, roots: { ws_example: s.project }, skillCache: cache,
        fetchSkills: async () => {
            throw Object.assign(new Error('no such version'), { code: 'incompatible_skill' });
        },
    });

    const reply = await h.execution.execute(frame({
        skill_resources: [{ skill_id: OTHER_SKILL_ID, digest }],
    }));

    assert.equal(reply.error_code, 'incompatible_skill');
    // The hook is an attempt, not an authority: the bytes it (did not) install
    // are still checked, so "it said it installed it" is not enough.
    assert.deepEqual(h.started, []);
});

test('a fetch that installs the wrong bytes is still refused by the cache', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const { cache } = skillCache();
    const declared = api.pkg.packageDigest(skillBundle());
    const h = build({
        ...s, roots: { ws_example: s.project }, skillCache: cache,
        fetchSkills: async () => {
            // A package that is not the version the frame named.
            cache.install(OTHER_SKILL_ID, api.pkg.packageDigest(skillBundle({
                'SKILL.md': Buffer.from('---\nname: other\n---\n'),
            })), skillBundle({ 'SKILL.md': Buffer.from('---\nname: other\n---\n') }));
        },
    });

    const reply = await h.execution.execute(frame({
        skill_resources: [{ skill_id: OTHER_SKILL_ID, digest: declared }],
    }));

    assert.equal(reply.error_code, 'incompatible_skill');
    assert.deepEqual(h.started, []);
});

test('a frame with no declared skills never calls the fetch hook', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    let calls = 0;
    const h = build({
        ...s, roots: { ws_example: s.project },
        fetchSkills: async () => { calls += 1; },
    });

    // Declaring nothing is the ordinary skill-less frame -- every v2 tool that
    // has nothing to do with skills -- so it must not pay for a delivery attempt.
    const bare = frame({ skill_resources: [] });
    const reply = await h.execution.execute(bare);

    assert.equal(reply.state, 'succeeded');
    assert.equal(calls, 0);
    assert.deepEqual(h.execution.skillRootsFor(bare), []);
});

test('a frame that declares a version it already holds still delivers it', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const { entries, digest } = sampleSkill();
    const { cache } = skillCache();
    cache.install(SAMPLE_SKILL_ID, digest, entries);
    let calls = 0;
    const h = build({
        ...s, roots: { ws_example: s.project }, skillCache: cache,
        fetchSkills: async () => { calls += 1; },
    });

    const reply = await h.execution.execute(frame());

    assert.equal(reply.state, 'succeeded');
    // The hook is the *delivery attempt*, and it runs for any frame that declares
    // a set -- what it must not do is re-download a version already held, which
    // is ``ensureSkillVersions``'s decision (tested in the transfer suite). The
    // run proceeds either way.
    assert.equal(calls, 1);
    assert.equal(lines(s.log), 1);
});

test('a skill resource without a digest is refused by name', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const { cache } = skillCache();
    const h = build({ ...s, roots: { ws_example: s.project }, skillCache: cache });

    const reply = await h.execution.execute(frame({
        skill_resources: [{ skill_id: OTHER_SKILL_ID }],
    }));

    assert.equal(reply.error_code, 'incompatible_skill');
    assert.deepEqual(h.started, []);
});

function lines(log) {
    if (!fs.existsSync(log)) return 0;
    return fs.readFileSync(log, 'utf8').split('\n').filter(Boolean).length;
}

async function waitForLines(log, count, timeoutMs = 4000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (lines(log) >= count) return true;
        await new Promise((r) => setTimeout(r, 10));
    }
    return lines(log) >= count;
}

// -- A14: a frame runs once, and the reply is the durable verdict ---------

test('a v2 frame runs once and produces the contract result frame', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: { ws_example: s.project } });

    const reply = await h.execution.execute(frame());

    assert.equal(reply.type, 'execution_result');
    assert.equal(reply.protocol_major, 2);
    assert.equal(reply.command_id, 'cmd_example');
    assert.equal(reply.run_id, 'run_example');
    assert.equal(reply.tool_call_id, 'call_example');
    assert.equal(reply.state, 'succeeded');
    assert.equal(reply.execution_phase, 'succeeded');
    assert.equal(reply.effects, 'completed');
    assert.equal(lines(s.log), 1);
    assert.deepEqual(h.started, ['cmd_example']);
    // The journal on disk is the reason the reply can be trusted.
    const journal = JSON.parse(fs.readFileSync(s.journalFile, 'utf8'));
    assert.equal(journal.entries.length, 1);
    assert.equal(journal.entries[0].receipt.outcome, 'succeeded');
});

test('A14: a redelivered frame returns the stored verdict and does not run again', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: { ws_example: s.project } });

    const first = await h.execution.execute(frame());
    const again = await h.execution.execute(frame());

    assert.equal(again.state, first.state);
    assert.equal(again.effects, first.effects);
    assert.equal(lines(s.log), 1, 'the effect happened exactly once');
});

test('A16: the same command id with different arguments is refused by name', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: { ws_example: s.project } });

    await h.execution.execute(frame());
    const tampered = await h.execution.execute(frame({
        arguments: { command: 'echo something-else', timeout: 5 },
        params_digest: `sha256:${'2'.repeat(64)}`,
    }));

    assert.equal(tampered.state, 'failed');
    assert.equal(tampered.error_code, 'command_conflict');
    assert.equal(tampered.effects, 'none', 'a refused frame claims no effect');
    assert.equal(lines(s.log), 1, 'the first payload is the only one that ran');
});

test('A16: a frame past its deadline is expired, not started late', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: { ws_example: s.project } });

    const reply = await h.execution.execute(frame({
        expires_at: '2026-09-30T20:59:59Z',
    }));

    assert.equal(reply.execution_phase, 'expired');
    assert.equal(reply.effects, 'none');
    assert.equal(lines(s.log), 0, 'a late frame is never started');
});

// -- refusals that must not even reach the journal ------------------------

test('a frame naming another device is refused without touching the journal', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: { ws_example: s.project } });

    const reply = await h.execution.execute(frame({ device_id: 'dev_someone_else' }));

    assert.equal(reply.state, 'failed');
    assert.equal(reply.error_code, 'permission_denied');
    assert.equal(reply.effects, 'none');
    assert.equal(fs.existsSync(s.journalFile), false, 'nothing was recorded');
    assert.equal(lines(s.log), 0);
});

test('a frame for an unbound session is refused as stale_context', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: {}, identity: null });

    const reply = await h.execution.execute(frame());

    assert.equal(reply.error_code, 'stale_context');
    assert.equal(lines(s.log), 0);
});

test('a frame that smuggles a cwd is refused by the contract, not run', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: { ws_example: s.project } });

    const reply = await h.execution.execute(frame({ cwd: '/etc' }));

    assert.equal(reply.state, 'failed');
    assert.equal(reply.error_code, 'invalid_request');
    assert.match(reply.error_message, /must not carry 'cwd'/);
    assert.equal(lines(s.log), 0);
});

test('an unknown tool is refused by name', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: { ws_example: s.project } });

    const reply = await h.execution.execute(frame({ tool: 'exec' }));

    assert.equal(reply.error_code, 'invalid_request');
    assert.equal(lines(s.log), 0);
});

test('a platform this build does not confine is refused, never translated', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({
        ...s, roots: { ws_example: s.project }, platform: 'win32',
    });

    const reply = await h.execution.execute(frame());

    assert.equal(reply.state, 'failed');
    assert.equal(reply.error_code, 'invalid_request');
    assert.match(reply.error_message, /unsupported on platform 'win32'/);
    assert.equal(lines(s.log), 0);
});

test('a frame for a workspace this install has not bound is refused', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: { ws_other: s.project } });

    const reply = await h.execution.execute(frame());

    assert.equal(reply.state, 'failed');
    assert.equal(reply.effects, 'none');
    assert.equal(lines(s.log), 0);
});

// -- A19: connection loss and liveness ------------------------------------

test('A19: while the connection is down a new frame is refused', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: { ws_example: s.project }, connected: () => false });

    const reply = await h.execution.execute(frame());

    assert.equal(reply.state, 'failed');
    assert.equal(reply.error_code, 'device_offline');
    assert.equal(reply.effects, 'none');
    assert.equal(lines(s.log), 0);
});

test('A19: past the liveness window the run is stopped and what it wrote is kept', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const reconciled = [];
    const h = build({
        ...s,
        roots: { ws_example: s.project },
        onReconcile: (entry, reason) => reconciled.push([entry.command_id, reason]),
    });
    const long = frame({ sleep_ms: 30_000 });
    const running = h.execution.execute(long);
    assert.ok(await waitForLines(s.log, 1));

    const abandoned = await h.execution.abandonStaleRuns({ disconnectedForMs: 30_001 });

    assert.deepEqual(abandoned.map((e) => e.command_id), ['cmd_example']);
    assert.deepEqual(h.running(), [], 'the process tree is really gone');
    assert.equal(abandoned[0].receipt.outcome, 'cancelled');
    assert.equal(abandoned[0].receipt.effects, 'unknown');
    assert.equal(reconciled.length, 1);
    assert.match(reconciled[0][1], /liveness window/);

    const reply = await running;
    assert.equal(reply.execution_phase, 'cancelled');
    assert.equal(reply.effects, 'unknown', 'a stopped run never promises a rollback');
});

// -- A18: cancellation ----------------------------------------------------

test('A18: cancel resolves only after the tree ended, and the reply says so', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: { ws_example: s.project } });
    const f = frame({ sleep_ms: 30_000 });

    const running = h.execution.execute(f);
    assert.ok(await waitForLines(s.log, 1));

    const cancelled = await h.execution.cancel(f);

    assert.equal(cancelled, true);
    assert.deepEqual(h.running(), [], 'cancel returning means the tree is gone');
    const reply = await running;
    assert.equal(reply.execution_phase, 'cancelled');
    assert.equal(reply.effects, 'unknown');
    assert.equal(lines(s.log), 1, 'what the script wrote before stopping is kept');
});

test('A18: cancelling a command this device is not running answers false', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: { ws_example: s.project } });

    assert.equal(await h.execution.cancel(frame({ command_id: 'cmd_nothing' })), false);
});

// -- A15: the start window ------------------------------------------------

test('A15: a crash after the start intent leaves outcome_unknown, never a re-run', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });

    // The intent on disk, then the process dies: exactly the window between
    // ``begin`` and the receipt. Written *before* the device starts, because
    // the journal is read once at construction -- a crash is not something that
    // happens while the process is running.
    const journal = new api.journal.JournalStore({ file: s.journalFile, now: () => NOW });
    journal.begin({
        journal_id: 'journal_lost', command_id: 'cmd_example',
        params_digest: frame().params_digest, origin: IDENTITY.origin,
        user_id: IDENTITY.user_id, tenant_id: IDENTITY.tenant_id,
        device_id: IDENTITY.device_id, workspace_id: 'ws_example',
        run_id: 'run_example', tool_call_id: 'call_example', tool: 'bash',
        grant_version: 3, started_at: NOW,
    }, {
        origin: IDENTITY.origin, user_id: IDENTITY.user_id, tenant_id: IDENTITY.tenant_id,
        device_id: IDENTITY.device_id, command_id: 'cmd_example',
    });

    const h = build({ ...s, roots: { ws_example: s.project } });
    const recovered = h.execution.recover();
    assert.equal(recovered.length, 1, 'the unfinished start is surfaced for a human');
    assert.equal(recovered[0].receipt.outcome, 'outcome_unknown');

    const reply = await h.execution.execute(frame());
    assert.equal(reply.state, 'failed', 'an unknown outcome is never stored as success');
    assert.equal(reply.execution_phase, 'outcome_unknown');
    assert.equal(reply.effects, 'unknown');
    assert.equal(lines(s.log), 0, 'nothing is retried to clear the unknown');
});

// -- 7.2 / 7.3: status answers from the journal ---------------------------

test('status answers queued before, running during, and the stored verdict after', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: { ws_example: s.project } });
    const f = frame({ sleep_ms: 5_000 });

    assert.deepEqual(h.execution.status(f), { state: 'queued' });

    const running = h.execution.execute(f);
    assert.ok(await waitForLines(s.log, 1));
    assert.equal(h.execution.status(f).state, 'running');

    await h.execution.cancel(f);
    await running;

    const after = h.execution.status(f);
    assert.equal(after.state, 'cancelled');
    assert.equal(after.effects, 'unknown');
});

// -- A20: serialisation and the outside-edit warning ---------------------

test('A20: two aliases of one real root serialise effectful frames', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({
        ...s,
        // Two workspace ids, one directory: the gate is keyed on the real root.
        roots: { ws_example: s.project, ws_alias: s.project },
    });
    const first = frame({ command_id: 'cmd_one' });
    const second = frame({
        command_id: 'cmd_two', run_id: 'run_2', tool_call_id: 'call_2',
        workspace_id: 'ws_alias',
    });

    await Promise.all([h.execution.execute(first), h.execution.execute(second)]);

    assert.equal(h.maxActive(), 1, 'effectful_per_project = 1 on the real root');
    assert.equal(lines(s.log), 2);
});

test('A20: an outside edit is reported as file_changed with partial effects', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const target = path.join(s.project, 'note.txt');
    fs.writeFileSync(target, 'first\n');
    const h = build({ ...s, roots: { ws_example: s.project } });

    // The model read the file; then someone else rewrote it before the edit.
    h.execution.runner.versions.noteServed(target);
    fs.writeFileSync(target, 'someone else changed this\n');

    const reply = await h.execution.execute(frame({
        tool: 'edit',
        arguments: { path: 'note.txt', old_text: 'first', new_text: 'second' },
    }));

    assert.equal(reply.error_code, 'file_changed');
    assert.equal(reply.effects, 'partial', 'the edit applied, on top of a moved view');
    assert.match(reply.error_message, /note\.txt was modified after you last read it/);
    assert.equal(lines(s.log), 1, 'the warning does not block the effect');
});

test('A20: a path outside the project is not treated as this frame stale', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const outside = path.join(s.dir, 'elsewhere.txt');
    fs.writeFileSync(outside, 'other\n');
    const h = build({ ...s, roots: { ws_example: s.project } });

    h.execution.runner.versions.noteServed(outside);
    fs.writeFileSync(outside, 'changed by someone else\n');

    const reply = await h.execution.execute(frame({
        tool: 'edit',
        arguments: { path: outside, old_text: 'other', new_text: 'x' },
    }));

    // The tool itself is the thing that refuses an out-of-project path; the
    // staleness check must not claim the edit is stale because of a file it has
    // no business touching.
    assert.equal(reply.error_code, undefined);
    assert.equal(reply.effects, 'completed');
});

// -- 7.1 / 7.6: the journal fails closed ---------------------------------

test('an unreadable journal refuses effectful work and quarantines the bytes', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    fs.writeFileSync(s.journalFile, '{not json');
    const h = build({ ...s, roots: { ws_example: s.project } });

    assert.equal(h.execution.degraded, true);
    const reply = await h.execution.execute(frame());

    assert.equal(reply.state, 'failed');
    assert.equal(reply.effects, 'none');
    assert.equal(lines(s.log), 0, 'a device that cannot rule out a double effect does not run');
    assert.equal(fs.existsSync(`${s.journalFile}.corrupt`), true);
});

test('the retention sweep leaves the unknown outcomes it still owes answers for', async () => {
    const s = scratch();
    fs.mkdirSync(s.project, { recursive: true });
    const h = build({ ...s, roots: { ws_example: s.project } });
    for (let n = 1; n <= 4; n += 1) {
        await h.execution.execute(frame({
            command_id: `cmd_${n}`, run_id: `run_${n}`, tool_call_id: `call_${n}`,
        }));
    }
    const removed = h.execution.sweep({ keepAtMost: 2 });
    const journal = JSON.parse(fs.readFileSync(s.journalFile, 'utf8'));

    assert.equal(removed.length, 2);
    assert.equal(journal.entries.length, 2);
    assert.ok(journal.pruned.length >= 2, 'each reclaimed receipt leaves a tombstone');
});
