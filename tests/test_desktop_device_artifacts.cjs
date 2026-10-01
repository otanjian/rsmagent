// The device half of local artifact references (task 9.1).
//
// Change ``align-desktop-project-execution-with-master``.
//
// The point of these tests is that the *device* names the file it just wrote,
// against its own real root, and nothing else. The server never stats a local
// path -- ``test_desktop_artifact_source.py`` covers that half -- so a result
// frame that arrives without a reference, or with one naming a file outside the
// project, is the failure this file exists to catch.
//
// The executor is real: each tool call writes a real file under a real project
// root, so the ``size`` and ``source_version`` in the reference are compared
// against the bytes actually on disk rather than against a fixture.
//
// Run: node --test tests/test_desktop_device_artifacts.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const crypto = require('node:crypto');
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
        artifact: require(path.join(distDir, 'artifact.js')),
        contract: require(path.join(distDir, 'contract.js')),
    };
});

const NOW = Date.parse('2026-09-30T21:00:00Z');

const IDENTITY = {
    origin: 'https://master.example',
    user_id: 'u-1',
    tenant_id: 't-1',
    device_id: 'dev_example',
};

function scratch() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'device-artifact-'));
    const project = path.join(dir, 'project');
    fs.mkdirSync(path.join(project, 'output'), { recursive: true });
    return { dir, project, journalFile: path.join(dir, 'journal.json') };
}

function frame(overrides = {}) {
    const base = JSON.parse(fs.readFileSync(samplePath, 'utf8'));
    // This suite is about the *artifact* half of a result frame: which file the
    // device names, against which root. A frame that declares skills would be
    // gated by task 8.9's cache check before the tool ever runs, so the gate
    // (covered by ``test_desktop_skill_transfer.cjs`` and
    // ``test_desktop_device_execution.cjs``) is deliberately not part of these
    // fixtures -- otherwise every assertion below would be reporting the skill
    // refusal rather than the artifact decision.
    delete base.skill_resources;
    return { ...base, ...overrides };
}

/** A write frame whose tool really creates the file it names. */
function writeFrame(target, { content = 'a,b\n1,2\n', tool = 'write' } = {}) {
    return frame({
        tool,
        arguments: { path: target, content },
    });
}

/**
 * A real executor: it writes the named file under the project root, exactly as
 * the sandboxed worker would, so the reference is checked against real bytes.
 */
function harness(project) {
    const runTool = async (request) => {
        const args = request.frame.arguments || {};
        const target = typeof args.path === 'string' && args.path
            ? path.resolve(project, args.path)
            : null;
        if (target) {
            fs.mkdirSync(path.dirname(target), { recursive: true });
            fs.writeFileSync(target, String(args.content ?? ''));
        }
        return { exit_code: 0, stdout: `ran ${request.frame.tool}` };
    };
    return { runTool, terminate: async () => {} };
}

function build({ project, journalFile, roots = {}, identity = IDENTITY }) {
    const h = harness(project);
    const execution = new api.execution.DeviceExecution({
        identity: () => identity,
        realRootOf: (workspaceId) => roots[workspaceId] ?? project,
        runTool: h.runTool,
        terminate: h.terminate,
        journalFile,
        now: () => NOW,
        platform: 'darwin',
        newJournalId: (() => { let n = 0; return () => `journal_${++n}`; })(),
    });
    return execution;
}

// -- the reference the device attaches to a successful write --------------

test('a successful write emits one desktop artifact naming the real file', async () => {
    const s = scratch();
    const execution = build(s);
    const content = 'name,qty\n螺丝,120\n';

    const reply = await execution.execute(
        writeFrame('output/开票清单.csv', { content }));

    assert.equal(reply.state, 'succeeded');
    assert.ok(Array.isArray(reply.artifacts), 'the result carries artifacts');
    assert.equal(reply.artifacts.length, 1);

    const [artifact] = reply.artifacts;
    assert.equal(artifact.source, 'desktop');
    assert.equal(artifact.device_id, 'dev_example');
    assert.equal(artifact.workspace_id, 'ws_example');
    assert.equal(artifact.run_id, 'run_example');
    assert.equal(artifact.tool_call_id, 'call_example');
    // Relative to the project root, never the device's real path.
    assert.equal(artifact.relative_path, 'output/开票清单.csv');
    assert.equal(artifact.file_name, '开票清单.csv');
    assert.equal(artifact.kind, 'text');
    assert.equal(artifact.size, Buffer.byteLength(content));
    assert.ok(!artifact.relative_path.includes(s.project),
        'the real root must not leak into the reference');
});

test('the source version is the content digest, not the file name', async () => {
    const s = scratch();
    const execution = build(s);
    const content = 'first\n';

    const reply = await execution.execute(writeFrame('output/report.txt', { content }));
    const expected = `sha256:${crypto.createHash('sha256').update(content).digest('hex')}`;

    assert.equal(reply.artifacts[0].source_version, expected);

    // Same name, different bytes -> different version. This is what stops a
    // cached preview from showing stale contents. A different command id, so
    // this is a new run rather than the journal replaying the first verdict.
    const second = await execution.execute(frame({
        command_id: 'cmd_example_2',
        run_id: 'run_example_2',
        tool_call_id: 'call_example_2',
        tool: 'write',
        arguments: { path: 'output/report.txt', content: 'second, longer\n' },
    }));
    assert.equal(second.artifacts[0].relative_path, 'output/report.txt');
    assert.notEqual(second.artifacts[0].source_version, expected);
});

test('the artifact id is deterministic for the same run, call and path', () => {
    const id = api.artifact.artifactId('run_example', 'call_example', 'output/a.csv');
    assert.equal(id, api.artifact.artifactId('run_example', 'call_example', 'output/a.csv'));
    assert.notEqual(id, api.artifact.artifactId('run_example', 'call_other', 'output/a.csv'));
    assert.ok(id.startsWith('art_'));
});

test('an edit of an existing file is a reference like any other', async () => {
    const s = scratch();
    fs.writeFileSync(path.join(s.project, 'output', 'notes.md'), '# old\n');
    const execution = build(s);

    const reply = await execution.execute(frame({
        tool: 'edit',
        arguments: { path: 'output/notes.md', content: '# new\n' },
    }));

    assert.equal(reply.artifacts.length, 1);
    assert.equal(reply.artifacts[0].relative_path, 'output/notes.md');
    assert.equal(reply.artifacts[0].kind, 'text');
});

// -- references are earned, not assumed ----------------------------------

test('a bash frame emits no artifact, even when it really wrote a file', async () => {
    const s = scratch();
    // This "script" parses its own ``--output`` argument and writes the file --
    // a script that genuinely produced a file. The device still claims nothing,
    // because a bash command's arguments do not name a file the way ``write``
    // does; guessing one would publish a card for a file this run may not have
    // created (the contract's reason for keeping the tool set explicit).
    const execution = new api.execution.DeviceExecution({
        identity: () => IDENTITY,
        realRootOf: () => s.project,
        runTool: async (request) => {
            const match = /--output "([^"]+)"/.exec(String(request.frame.arguments.command));
            assert.ok(match, 'the test script must have an output to write');
            const target = path.resolve(s.project, match[1]);
            fs.mkdirSync(path.dirname(target), { recursive: true });
            fs.writeFileSync(target, 'generated\n');
            return { exit_code: 0, stdout: `已生成 ${match[1]}` };
        },
        terminate: async () => {},
        journalFile: s.journalFile,
        now: () => NOW,
        platform: 'darwin',
    });

    const reply = await execution.execute(frame({
        arguments: { command: 'python report.py --output "output/开票清单.xlsx"', timeout: 120 },
    }));

    assert.equal(reply.state, 'succeeded');
    assert.ok(fs.existsSync(path.join(s.project, 'output', '开票清单.xlsx')),
        'the script really did write the file');
    assert.equal(reply.artifacts, undefined,
        'a script names no file in its arguments, so the device must not guess one');
});

test('a write that a failed run never completed emits no artifact', async () => {
    const s = scratch();
    const execution = new api.execution.DeviceExecution({
        identity: () => IDENTITY,
        realRootOf: () => s.project,
        // The worker refuses: nothing was written, so nothing is claimed.
        runTool: async () => { throw new Error('worker refused'); },
        terminate: async () => {},
        journalFile: s.journalFile,
        now: () => NOW,
        platform: 'darwin',
    });

    const reply = await execution.execute(writeFrame('output/never.csv'));

    assert.notEqual(reply.execution_phase, 'succeeded');
    assert.equal(reply.artifacts, undefined);
    assert.ok(!fs.existsSync(path.join(s.project, 'output', 'never.csv')));
});

test('a write run that failed after writing the file emits no artifact', async () => {
    const s = scratch();
    // The tool wrote the file and *then* failed (a crash while flushing, a
    // non-zero exit from the wrapper). The bytes are on disk, but they are not
    // the output the run claimed, so no card may be published for them.
    const execution = new api.execution.DeviceExecution({
        identity: () => IDENTITY,
        realRootOf: () => s.project,
        runTool: async (request) => {
            const args = request.frame.arguments || {};
            const target = path.resolve(s.project, String(args.path));
            fs.mkdirSync(path.dirname(target), { recursive: true });
            fs.writeFileSync(target, String(args.content ?? ''));
            return { exit_code: 3, stdout: '', stderr: 'crashed after writing' };
        },
        terminate: async () => {},
        journalFile: s.journalFile,
        now: () => NOW,
        platform: 'darwin',
    });

    const reply = await execution.execute(writeFrame('output/half.csv'));

    assert.equal(reply.execution_phase, 'failed');
    assert.ok(fs.existsSync(path.join(s.project, 'output', 'half.csv')),
        'the partial file is really there');
    assert.equal(reply.artifacts, undefined,
        'a failed run must not have a card published for what it left behind');
});

test('a write that escapes the project names no file', async () => {
    const s = scratch();
    const execution = build(s);

    const reply = await execution.execute(writeFrame('../escaped.csv', {
        content: 'outside\n',
    }));

    assert.equal(reply.artifacts, undefined,
        'a path outside the workspace is not this run\'s artifact');
});

test('a frame whose file never reached disk emits no artifact', async () => {
    const s = scratch();
    const execution = new api.execution.DeviceExecution({
        identity: () => IDENTITY,
        realRootOf: () => s.project,
        // Claims success without writing, which is exactly the case the
        // "verify before publish" rule exists for.
        runTool: async () => ({ exit_code: 0, stdout: 'pretended' }),
        terminate: async () => {},
        journalFile: s.journalFile,
        now: () => NOW,
        platform: 'darwin',
    });

    const reply = await execution.execute(writeFrame('output/ghost.csv'));

    assert.equal(reply.artifacts, undefined);
});

test('a missing workspace root refuses to name anything', async () => {
    const s = scratch();
    const execution = new api.execution.DeviceExecution({
        identity: () => IDENTITY,
        realRootOf: () => null,
        runTool: async () => ({ exit_code: 0, stdout: 'wrote' }),
        terminate: async () => {},
        journalFile: s.journalFile,
        now: () => NOW,
        platform: 'darwin',
    });

    const reply = await execution.execute(writeFrame('output/a.csv'));
    assert.equal(reply.artifacts, undefined);
});

// -- the reference satisfies the frozen contract -------------------------

test('the emitted reference carries every field the contract requires', async () => {
    const s = scratch();
    const execution = build(s);
    const reply = await execution.execute(writeFrame('output/report.xlsx', {
        content: 'x'.repeat(64),
    }));

    const required = [
        'source', 'artifact_id', 'device_id', 'workspace_id', 'run_id',
        'tool_call_id', 'relative_path', 'file_name', 'kind', 'size',
        'source_version',
    ];
    const artifact = reply.artifacts[0];
    for (const key of required) {
        assert.ok(artifact[key] !== undefined && artifact[key] !== '',
            `artifact is missing '${key}'`);
    }
    // Every kind is one the contract knows.
    assert.ok(['text', 'office', 'image', 'pdf', 'archive', 'binary']
        .includes(artifact.kind));
    // A symbol in the reference is what the frozen result frame forbids.
    assert.equal(typeof artifact.size, 'number');
    assert.ok(!path.isAbsolute(artifact.relative_path));
});

test('a large file uses a cheap version rather than hashing gigabytes', () => {
    const s = scratch();
    const target = path.join(s.project, 'output', 'big.bin');
    const size = api.artifact.SOURCE_VERSION_DIGEST_MAX_BYTES + 1;
    fs.writeFileSync(target, Buffer.alloc(size));

    const built = api.artifact.buildArtifact({
        realRoot: s.project,
        workspaceId: 'ws_example',
        deviceId: 'dev_example',
        runId: 'run_example',
        toolCallId: 'call_example',
        candidate: target,
    });

    assert.ok(built.source_version.startsWith('stat:'),
        'a multi-megabyte file must not be hashed to render a preview');
    assert.equal(built.kind, 'binary');
    assert.equal(built.size, size);

    // At or below the threshold the version is still a real content digest.
    const small = path.join(s.project, 'output', 'small.bin');
    fs.writeFileSync(small, 'tiny');
    const hashed = api.artifact.buildArtifact({
        realRoot: s.project,
        workspaceId: 'ws_example',
        deviceId: 'dev_example',
        runId: 'run_example',
        toolCallId: 'call_example',
        candidate: small,
    });
    assert.ok(hashed.source_version.startsWith('sha256:'));
});

test('a symlink pointing outside the project is not a local reference', async () => {
    const s = scratch();
    const outside = path.join(s.dir, 'outside.csv');
    fs.writeFileSync(outside, 'secret\n');
    fs.symlinkSync(outside, path.join(s.project, 'output', 'link.csv'));
    const execution = build(s);

    const reply = await execution.execute(writeFrame('output/link.csv', {
        content: 'secret\n',
    }));

    assert.equal(reply.artifacts, undefined,
        'resolving the symlink must land inside the project, or there is no reference');
});

test('a file reached through a symlinked directory is not in the project', () => {
    const s = scratch();
    const outsideDir = path.join(s.dir, 'outside');
    fs.mkdirSync(outsideDir, { recursive: true });
    fs.writeFileSync(path.join(outsideDir, 'secret.csv'), 'secret\n');
    // The *final* component is a real regular file, so the ``lstat`` check alone
    // lets this through -- only collapsing the path and comparing it against the
    // real root catches it.
    fs.symlinkSync(outsideDir, path.join(s.project, 'linkdir'));

    const built = api.artifact.buildArtifact({
        realRoot: s.project,
        workspaceId: 'ws_example',
        deviceId: 'dev_example',
        runId: 'run_example',
        toolCallId: 'call_example',
        candidate: 'linkdir/secret.csv',
    });

    assert.equal(built, null,
        'the real path leaves the project, so there is no reference to publish');
});

test('an absolute path outside the project is not a reference', () => {
    const s = scratch();
    const outside = path.join(s.dir, 'outside.csv');
    fs.writeFileSync(outside, 'secret\n');

    const built = api.artifact.buildArtifact({
        realRoot: s.project,
        workspaceId: 'ws_example',
        deviceId: 'dev_example',
        runId: 'run_example',
        toolCallId: 'call_example',
        candidate: outside,
    });

    assert.equal(built, null);
});

test('a directory is not an artifact', () => {
    const s = scratch();

    const built = api.artifact.buildArtifact({
        realRoot: s.project,
        workspaceId: 'ws_example',
        deviceId: 'dev_example',
        runId: 'run_example',
        toolCallId: 'call_example',
        candidate: path.join(s.project, 'output'),
    });

    assert.equal(built, null, 'a reference names a file, never a directory');
});
