// The v2 project-execution contract, checked from the client side.
//
// Change ``align-desktop-project-execution-with-master``, task 6.1. The contract
// lives as data in ``contracts/desktop/v2.json`` and the client compiles its
// values in; this file is what keeps the two from drifting (a phase name or a
// timeout changes in one place and both sides' tests fail). It drives the real
// compiled module, so the client's *own* checks -- the ones that decide whether
// a frame may be sent at all -- are exercised rather than asserted by keyword.
//
// Run: node --test tests/test_desktop_execution_contract.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const contractPath = path.join(root, 'contracts', 'desktop', 'v2.json');
const samplesDir = path.join(root, 'contracts', 'desktop', 'samples', 'v2');
const srcDir = path.join(desktop, 'src', 'main', 'project-execution');
const compiled = path.join(desktop, 'dist', 'main', 'project-execution', 'contract.js');

let contract;
let execution;

function needsBuild() {
    if (!fs.existsSync(compiled)) return true;
    const newest = Math.max(
        ...fs.readdirSync(srcDir).filter((n) => n.endsWith('.ts'))
            .map((n) => fs.statSync(path.join(srcDir, n)).mtimeMs),
    );
    return newest > fs.statSync(compiled).mtimeMs;
}

function sample(name) {
    return JSON.parse(fs.readFileSync(path.join(samplesDir, name), 'utf8'));
}

before(() => {
    if (needsBuild()) {
        execFileSync('npm', ['run', 'build:main'], {
            cwd: desktop,
            stdio: 'inherit',
            // ``npm`` is ``npm.cmd`` on Windows, and ``execFileSync`` without a
            // shell fails with ``spawnSync npm ENOENT`` before the build starts --
            // which made every test in this file fail for a reason unrelated to
            // what it tests. Same flag as the sibling suites.
            shell: process.platform === 'win32',
        });
    }
    contract = JSON.parse(fs.readFileSync(contractPath, 'utf8'));
    execution = require(compiled);
});

// ---------------------------------------------------------------------------
// Cross-language agreement
// ---------------------------------------------------------------------------

test('the client protocol matches the contract document', () => {
    assert.equal(execution.PROJECT_EXECUTION_PROTOCOL.major, contract.protocol.major);
    assert.equal(execution.PROJECT_EXECUTION_PROTOCOL.minor, contract.protocol.minor);
    assert.equal(execution.PROJECT_EXECUTION_PROTOCOL.required, contract.protocol.required);
    assert.equal(execution.PROJECT_EXECUTION_PROTOCOL.required, false,
        'v2 must stay optional so a v1-only peer keeps working');
    assert.equal(execution.PROJECT_EXECUTION_PROTOCOL.major, 2);
});

test('the client frame types, tools and limits match the document', () => {
    assert.deepEqual([...execution.EXECUTION_FRAME_TYPES], contract.frames.types);
    assert.deepEqual([...execution.EXECUTION_TOOLS], contract.tools.required);
    assert.deepEqual([...execution.EFFECTFUL_TOOLS], contract.tools.effectful);
    assert.deepEqual([...execution.READONLY_TOOLS], contract.tools.readonly);
    assert.deepEqual([...execution.SCRIPT_TOOLS], contract.tools.script_tools);
    assert.equal(execution.TOOL_SCHEMA_VERSION, contract.tools.schema_version);
    assert.equal(execution.START_PERMIT_TTL_SECONDS, contract.limits.start_permit_ttl_seconds);
    assert.equal(execution.START_PERMIT_SINGLE_USE, contract.limits.start_permit_single_use);
    assert.equal(execution.OUTCOME_UNKNOWN_CODE, contract.phases.outcome_unknown_code);
    assert.equal(execution.ARTIFACT_PROTOCOL, contract.capability.artifact_protocol);
    assert.equal(execution.EXECUTION_CAPABILITY_KEY, 'project_execution');
    assert.deepEqual([...execution.FORBIDDEN_FRAME_FIELDS],
        [...contract.frames.execute_tool.forbidden]);
});

test('the client limits match the document', () => {
    // Inherited bounds live in v1.json; the client must carry those values too,
    // not a second copy that can drift.
    const v1 = JSON.parse(fs.readFileSync(path.join(root, 'contracts', 'desktop', 'v1.json'), 'utf8'));
    const inherited = new Set(contract.limits.reuse_v1);
    for (const [name, value] of Object.entries(execution.EXECUTION_LIMITS)) {
        const expected = inherited.has(name) ? v1.limits[name] : contract.limits[name];
        assert.equal(value, expected, `${name} drifted`);
    }
    for (const name of contract.limits.reuse_v1) {
        assert.ok(name in execution.EXECUTION_LIMITS,
            `${name} is inherited but missing from the client limits`);
    }
    assert.equal(execution.EXECUTION_LIMITS.script_timeout_default_seconds, 120);
    assert.equal(execution.EXECUTION_LIMITS.script_timeout_max_seconds, 600);
    assert.equal(execution.EXECUTION_LIMITS.run_liveness_seconds, 30);
});

test('the client phase table matches the document', () => {
    assert.deepEqual([...execution.EXECUTION_PHASES], contract.phases.names);
    assert.deepEqual([...execution.TERMINAL_PHASES], contract.phases.terminal);
    assert.deepEqual([...execution.CANCELLING_OVERRIDES], contract.phases.cancelling_overrides);
    assert.deepEqual([...execution.EXECUTION_EFFECTS], contract.effects.names);
    for (const [state, phase] of Object.entries(contract.phases.from_state)) {
        assert.equal(execution.STATE_TO_PHASE[state], phase, `state ${state}`);
    }
    // Every state the server can report has a phase here, and nothing extra.
    assert.deepEqual(Object.keys(execution.STATE_TO_PHASE).sort(),
        Object.keys(contract.phases.from_state).sort());
    assert.deepEqual(Object.keys(execution.STATE_TO_PHASE).sort(),
        ['acknowledged', 'cancelled', 'dispatched', 'expired', 'failed', 'queued',
            'running', 'succeeded']);
});

// ---------------------------------------------------------------------------
// The shipped samples
// ---------------------------------------------------------------------------

test('the shipped execute_tool sample is one this client would send', () => {
    const frame = sample('execute_tool.valid.json');
    assert.deepEqual(execution.validateExecuteFrame(frame, 'posix'), []);
    assert.equal(frame.tool_schema_version, execution.TOOL_SCHEMA_VERSION);
});

test('the shipped result sample projects onto the client phase table', () => {
    const result = sample('execution_result.valid.json');
    assert.equal(execution.phaseFor(result.state, {
        cancelRequested: result.cancel_requested,
        errorCode: result.error_code || null,
    }), result.execution_phase);
    assert.equal(execution.isTerminalPhase(result.execution_phase), true);
    assert.equal(execution.effectsFor(result.execution_phase, result.error_code || null),
        result.effects);
});

// ---------------------------------------------------------------------------
// The canonical digest, agreed with the Python implementation
// ---------------------------------------------------------------------------

test('the client computes the same params_digest as the server', () => {
    // One fixture, two implementations: the Python side pins the same value in
    // tests/test_desktop_execution_payload.py. A change to either side's
    // canonicalization fails one of the two tests.
    const fixture = sample('digest_envelope.valid.json');
    assert.ok(fixture.digest.startsWith('sha256:'));
    assert.equal(execution.paramsDigest(fixture.envelope), fixture.digest);
});

test('the canonical form is key-order independent and whitespace free', () => {
    const fixture = sample('digest_envelope.valid.json');
    const reversed = {};
    for (const key of Object.keys(fixture.envelope).reverse()) {
        reversed[key] = fixture.envelope[key];
    }
    assert.equal(execution.paramsDigest(reversed), fixture.digest);
    // An exact literal, checked against the identical string on the Python side
    // (tests/test_desktop_execution_payload.py): proves key sorting, no
    // structural whitespace, and that spaces *inside* strings survive.
    assert.equal(
        execution.canonicalJson({ b: 1, a: { d: [1, 2], c: 'x y' } }),
        '{"a":{"c":"x y","d":[1,2]},"b":1}',
    );
});

test('a non-integer number is refused instead of serialized', () => {
    // 1.0 and 1 (and Infinity) can differ between the two languages; two
    // different digests for one command is exactly what the digest prevents.
    const fixture = sample('digest_envelope.valid.json');
    const changed = { ...fixture.envelope, arguments: { command: 'echo', timeout: 1.5 } };
    assert.throws(() => execution.paramsDigest(changed), /non-integer/);
});

test('an absent optional field cannot collide with a forgotten one', () => {
    const fixture = sample('digest_envelope.valid.json');
    const without = { ...fixture.envelope, skill_resources: [] };
    assert.notEqual(execution.paramsDigest(without), fixture.digest);
    assert.deepEqual(execution.canonicalEnvelope(without).skill_resources, []);
});

test('every covered field changes the digest', () => {
    const fixture = sample('digest_envelope.valid.json');
    const alternatives = {
        tool: 'read',
        tool_schema_version: 2,
        arguments: { command: 'echo 2' },
        run_id: 'run_other',
        tool_call_id: 'call_other',
        session_id: 'sess_other',
        agent_id: 'agent_other',
        origin: 'https://other.invalid',
        binding_id: 'bind_other',
        workspace_id: 'ws_other',
        device_id: 'dev_other',
        grant_version: 4,
        selection_generation: 9,
        skill_resources: [{ skill_id: 'example-xlsx', digest: 'sha256:' + '1'.repeat(64) }],
    };
    for (const [key, value] of Object.entries(alternatives)) {
        assert.notEqual(
            execution.paramsDigest({ ...fixture.envelope, [key]: value }),
            fixture.digest,
            key,
        );
    }
    assert.deepEqual([...execution.DIGEST_FIELDS], [
        'tool', 'tool_schema_version', 'arguments', 'run_id', 'tool_call_id',
        'session_id', 'agent_id', 'origin', 'binding_id', 'workspace_id',
        'device_id', 'grant_version', 'selection_generation', 'skill_resources',
    ]);
});

test('the shipped capability and hello samples parse', () => {    const block = sample('meta_project_execution.valid.json');
    const parsed = execution.parseExecutionCapability({ project_execution: block });
    assert.ok(parsed);
    assert.equal(parsed.available, true);
    assert.equal(parsed.protocol_major, 2);
    const hello = sample('hello_execution.valid.json');
    assert.deepEqual(hello.tools, [...execution.EXECUTION_TOOLS]);
    assert.equal(execution.parseExecutionCapability({}), null);
    assert.equal(execution.parseExecutionCapability(null), null);
    assert.equal(execution.parseExecutionCapability({ project_execution: { available: 'yes' } }),
        null);
});

// ---------------------------------------------------------------------------
// Rules the client must not be able to forget
// ---------------------------------------------------------------------------

test('a frame that names its own cwd or module never leaves the client', () => {
    const frame = sample('execute_tool.valid.json');
    for (const key of contract.frames.execute_tool.forbidden) {
        const problems = execution.validateExecuteFrame({ ...frame, [key]: '/tmp/x' }, 'posix');
        assert.ok(problems.some((p) => p.includes(key)), key);
    }
    const withCwdArg = {
        ...frame,
        arguments: { ...frame.arguments, cwd: '/etc' },
    };
    const problems = execution.validateExecuteFrame(withCwdArg, 'posix');
    assert.ok(problems.some((p) => p.includes("arguments must not carry 'cwd'")));
});

test('an unknown tool or a v1 protocol major is refused', () => {
    const frame = sample('execute_tool.valid.json');
    assert.ok(execution.validateExecuteFrame({ ...frame, tool: 'shell_exec' }, 'posix')
        .some((p) => p.includes('unknown tool')));
    assert.ok(execution.validateExecuteFrame({ ...frame, protocol_major: 1 }, 'posix')
        .some((p) => p.includes('protocol_major')));
});

test('a missing correlation id or a bad digest is refused', () => {
    const frame = sample('execute_tool.valid.json');
    for (const key of ['command_id', 'run_id', 'tool_call_id', 'workspace_id',
        'connection_epoch', 'params_digest']) {
        const broken = { ...frame };
        delete broken[key];
        assert.ok(execution.validateExecuteFrame(broken, 'posix').length > 0, key);
    }
    assert.ok(execution.validateExecuteFrame({ ...frame, params_digest: 'md5:abc' }, 'posix')
        .some((p) => p.includes('params_digest')));
    assert.equal(execution.looksLikeDigest('sha256:' + 'a'.repeat(64)), true);
    assert.equal(execution.looksLikeDigest('sha256:zz'), false);
});

test('an unbounded script timeout is refused before it is sent', () => {
    const frame = sample('execute_tool.valid.json');
    const tooLong = {
        ...frame,
        arguments: { ...frame.arguments, timeout: execution.EXECUTION_LIMITS.script_timeout_max_seconds + 1 },
    };
    assert.ok(execution.validateExecuteFrame(tooLong, 'posix')
        .some((p) => p.includes('timeout')));
    const empty = { ...frame, arguments: { command: '   ', timeout: 10 } };
    assert.ok(execution.validateExecuteFrame(empty, 'posix')
        .some((p) => p.includes('command')));
    const atTheLimit = {
        ...frame,
        arguments: { ...frame.arguments, timeout: execution.EXECUTION_LIMITS.script_timeout_max_seconds },
    };
    assert.deepEqual(execution.validateExecuteFrame(atTheLimit, 'posix'), []);
});

test('windows offers no tool at all until its launcher is accepted', () => {
    const frame = sample('execute_tool.valid.json');
    assert.deepEqual(execution.supportedTools('win32'), []);
    assert.deepEqual(execution.supportedTools('posix'), [...execution.EXECUTION_TOOLS]);
    assert.ok(execution.validateExecuteFrame({ ...frame, tool: 'read', arguments: { path: 'a.txt' } },
        'win32').some((p) => p.includes('unsupported on platform')));
    assert.equal(contract.platforms.win32.supported, false);
});

// ---------------------------------------------------------------------------
// Task 3.5: the frame is judged by its *own* major.
//
// Before this, every execute frame was validated against v2 and a v3 frame was
// refused with "protocol_major must be 2" -- a version-awareness gap reported as a
// malformed frame, which is why the device side could not run a v3 command at all.
// ---------------------------------------------------------------------------

/** The shipped v3 sample, which the contract itself validates. */
function v3Sample() {
    return JSON.parse(fs.readFileSync(
        path.join(root, 'contracts', 'desktop', 'samples', 'v3', 'execute_tool.valid.json'),
        'utf8'));
}

test('a v3 frame is judged by v3, not refused for naming a major it may name', () => {
    // The major is the *connection's*, so it is passed: a frame does not get to
    // choose which rules judge it.
    const problems = execution.validateExecuteFrame(v3Sample(), 'posix', 3);
    assert.deepEqual(problems, []);
});

test('a v2 frame is judged exactly as before', () => {
    // The regression guard for the refactor: the shared implementation must not
    // change a single v2 answer, including its message text.
    const v2Sample = JSON.parse(fs.readFileSync(
        path.join(root, 'contracts', 'desktop', 'samples', 'v2', 'execute_tool.valid.json'),
        'utf8'));
    assert.deepEqual(execution.validateExecuteFrame(v2Sample, 'posix', 2), []);
    assert.deepEqual(execution.validateExecuteFrame(v2Sample, 'posix'), [],
        'omitting the agreed major keeps v2, which is what every old caller does');

    const wrongMajor = { ...v2Sample, protocol_major: 3 };
    assert.deepEqual(execution.validateExecuteFrame(wrongMajor, 'posix', 2),
        ['protocol_major must be 2']);
});

test('a frame does not get to choose which rules judge it', () => {
    // The reason the agreed major is a parameter rather than read from the frame:
    // if the frame chose, a v3 frame on a v2 connection would be checked against
    // v3 and pass. Here the connection agreed v2 and the frame claims v3, so it is
    // judged by **v2** and the mismatch is reported.
    const frame = { ...v3Sample(), protocol_major: 3 };
    const problems = execution.validateExecuteFrame(frame, 'posix', 2);
    assert.ok(problems.includes('protocol_major must be 2'), problems.join('; '));
});

test('a major this build cannot serve is refused by name, never guessed at', () => {
    const problems = execution.validateExecuteFrame(
        { ...v3Sample(), protocol_major: 9 }, 'posix', 9);
    assert.equal(problems.length, 1);
    assert.match(problems[0], /protocol_major must be one of 3, 2/);
    assert.match(problems[0], /got 9/);
});

test('a v3 frame must name the operations it is spending', () => {
    // Required by v3 and unknown to v2, so the difference has to come from the
    // contract rather than from a hard-coded list.
    const frame = { ...v3Sample() };
    delete frame.operations;
    const problems = execution.validateExecuteFrame(frame, 'posix', 3);
    assert.ok(problems.some((p) => p.includes("missing 'operations'")), problems.join('; '));
});

test('v3 admits win32 as a platform but not every tool on it', () => {
    // The distinction that platform admission alone would miss: v3's
    // ``PLATFORM_SUPPORT.win32`` is true while ``PLATFORM_TOOLS.win32`` names only
    // the file tools, because ``launcher_kinds.scripts`` is deliberately empty
    // there. A check that asked only "is this platform supported?" would let a
    // Windows v3 frame name a shell tool and pass.
    const fileTool = v3Sample();
    assert.deepEqual(execution.validateExecuteFrame(fileTool, 'win32', 3), [],
        'the Windows file tools are the ones v3 offers there');

    // The v2 sample's tool is ``bash``, which win32 has no launcher for -- in
    // either major.
    const bash = JSON.parse(fs.readFileSync(
        path.join(root, 'contracts', 'desktop', 'samples', 'v2', 'execute_tool.valid.json'),
        'utf8'));
    const v3Bash = { ...bash, protocol_major: 3, operations: ['scripts'] };
    const problems = execution.validateExecuteFrame(v3Bash, 'win32', 3);
    assert.deepEqual(problems, ["tool 'bash' is unsupported on platform 'win32'"]);

    // And v2's own answer is unchanged, message included.
    assert.deepEqual(execution.validateExecuteFrame(bash, 'win32', 2),
        ["tool 'bash' is unsupported on platform 'win32'"]);
});

test('a v3 run_script is asked for its language and script, not a command string', () => {
    // v3 splits the v2 ``command`` into a declared runtime plus the script, which
    // is what makes "which language" a fact rather than something inferred.
    const frame = {
        ...v3Sample(),
        tool: 'run_script',
        operations: ['scripts'],
        arguments: { language: 'python', script: 'print(1)' },
    };
    assert.deepEqual(execution.validateExecuteFrame(frame, 'posix', 3), []);

    const missingLanguage = { ...frame, arguments: { script: 'print(1)' } };
    const problems = execution.validateExecuteFrame(missingLanguage, 'posix', 3);
    assert.ok(problems.some((p) => p.includes('run_script.arguments.language')),
        problems.join('; '));
});

// ---------------------------------------------------------------------------
// Negotiation and phases
// ---------------------------------------------------------------------------

test('an older server leaves the entry point unavailable, never downgraded', () => {
    assert.deepEqual(execution.negotiateProjectExecution({ bridge: { major: 1 } }),
        { available: false, reason: 'not_implemented' });
    assert.deepEqual(execution.negotiateProjectExecution(null),
        { available: false, reason: 'not_implemented' });
    assert.deepEqual(execution.negotiateProjectExecution({ project_execution: { major: 1 } }),
        { available: false, reason: 'protocol_incompatible' });
    assert.deepEqual(execution.negotiateProjectExecution({ project_execution: { major: 2 } }),
        { available: true, reason: 'available', major: 2 });
    // v3 is reachable too (task 3.5). The chosen major comes back so no caller has
    // to re-derive it -- reading it twice is how the negotiation and the frame
    // validation would come to disagree.
    assert.deepEqual(execution.negotiateProjectExecution({ project_execution: { major: 3 } }),
        { available: true, reason: 'available', major: 3 });
    // A major this build cannot serve is still refused by name, including one
    // *newer* than v3: "the server named a number" is not agreement.
    assert.deepEqual(execution.negotiateProjectExecution({ project_execution: { major: 4 } }),
        { available: false, reason: 'protocol_incompatible' });
});

test('cancelling is a projection, and a terminated run never claims a rollback', () => {
    const projection = execution.phaseFor;
    assert.equal(projection('running'), 'running');
    assert.equal(projection('running', { cancelRequested: true }), 'cancelling');
    assert.equal(projection('queued', { cancelRequested: true }), 'cancelling');
    assert.equal(projection('succeeded', { cancelRequested: true }), 'succeeded');
    assert.equal(projection('planning'), null);
    assert.equal(execution.effectsFor('cancelled'), 'unknown');
    assert.equal(execution.effectsFor('failed', 'permission_denied'), 'none');
    assert.equal(execution.effectsFor('failed'), 'unknown');
    assert.equal(execution.effectsFor('failed', 'deadline_exceeded'), 'partial');
    assert.equal(execution.effectsFor('succeeded'), 'completed');
    assert.equal(execution.effectsFor('running'), 'none');
    assert.equal(execution.isTerminalPhase('cancelling'), false);
    assert.equal(execution.isTerminalPhase('outcome_unknown'), true);
});
