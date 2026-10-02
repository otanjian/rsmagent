// Local script execution boundary: profile, launch plan, environment, lifecycle.
//
// Run: node --test tests/test_desktop_local_execution.cjs
//
// Two claims are checked here, and the second is the one that matters:
//
//   1. the launcher refuses to run a script where no boundary has been probed,
//      instead of falling back to an unconfined process;
//   2. on a platform that *has* been probed, the boundary actually holds --
//      asserted by trying to read and write outside the project through a real
//      sandboxed process, not by inspecting the profile text.

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync, spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const dist = path.join(desktop, 'dist', 'main', 'local-execution');

let sandbox;
let env;
let sessionMod;
let interpreterMod;
let launchMod;

before(() => {
  const sources = [
    path.join(desktop, 'src', 'main', 'local-execution', 'sandbox.ts'),
    path.join(desktop, 'src', 'main', 'local-execution', 'env.ts'),
    path.join(desktop, 'src', 'main', 'local-execution', 'worker-session.ts'),
    path.join(desktop, 'src', 'main', 'local-execution', 'interpreter.ts'),
    path.join(desktop, 'src', 'main', 'local-execution', 'launch.ts'),
  ];
  const compiled = [
    path.join(dist, 'sandbox.js'),
    path.join(dist, 'env.js'),
    path.join(dist, 'worker-session.js'),
    path.join(dist, 'interpreter.js'),
    path.join(dist, 'launch.js'),
  ];
  const stale = sources.some(
    (src, i) => !fs.existsSync(compiled[i]) || fs.statSync(compiled[i]).mtimeMs < fs.statSync(src).mtimeMs,
  );
  if (stale) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
  sandbox = require(path.join(dist, 'sandbox.js'));
  env = require(path.join(dist, 'env.js'));
  sessionMod = require(path.join(dist, 'worker-session.js'));
  interpreterMod = require(path.join(dist, 'interpreter.js'));
  launchMod = require(path.join(dist, 'launch.js'));
});

function paths(overrides = {}) {
  return {
    projectRoot: '/private/tmp/cow/proj',
    tempRoot: '/private/tmp/cow/tmp',
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Support decisions
// ---------------------------------------------------------------------------

test('an unprobed platform refuses rather than running unconfined', () => {
  const support = sandbox.resolveScriptSupport({
    platform: 'win32',
    paths: paths(),
    pythonPath: 'C:\\app\\python.exe',
    seatbeltAvailable: false,
  });
  assert.equal(support.supported, false);
  assert.equal(support.code, 'unsupported_platform');
  assert.match(support.reason, /disabled/);
  // The refusal must not read as a temporary permission problem.
  assert.match(support.reason, /not a permissions problem/);
});

test('a linux build refuses too', () => {
  const support = sandbox.resolveScriptSupport({
    platform: 'linux',
    paths: paths(),
    pythonPath: '/usr/bin/python3',
  });
  assert.equal(support.supported, false);
  assert.equal(support.code, 'unsupported_platform');
});

test('no sandbox-exec on this mac means no script support', () => {
  const support = sandbox.resolveScriptSupport({
    platform: 'darwin',
    paths: paths(),
    pythonPath: '/app/python3',
    seatbeltAvailable: false,
  });
  assert.equal(support.supported, false);
  assert.equal(support.code, 'sandbox_unavailable');
});

test('a missing interpreter means no script support, not a broken launch', () => {
  const support = sandbox.resolveScriptSupport({
    platform: 'darwin',
    paths: paths(),
    pythonPath: undefined,
    seatbeltAvailable: true,
  });
  assert.equal(support.supported, false);
  assert.equal(support.code, 'python_not_found');
});

test('a probed mac reports the real capabilities, not a generic claim', () => {
  const support = sandbox.resolveScriptSupport({
    platform: 'darwin',
    paths: paths({ skillRoots: ['/private/tmp/cow/skills'] }),
    pythonPath: '/app/python3',
    seatbeltAvailable: true,
  });
  assert.equal(support.supported, true);
  assert.equal(support.kind, 'seatbelt');
  assert.equal(support.capabilities.network, false);
  assert.equal(support.capabilities.readsConfined, true);
  assert.deepEqual(support.capabilities.writable, [
    '/private/tmp/cow/proj',
    '/private/tmp/cow/tmp',
  ]);
  assert.deepEqual(support.capabilities.readable, [
    '/private/tmp/cow/proj',
    '/private/tmp/cow/skills',
  ]);
  const described = sandbox.describeScriptCapabilities(support);
  assert.match(described, /Isolation: /);
  assert.match(described, /Network: not allowed/);
  // The model repeats these strings, so the metadata caveat must be in them.
  assert.match(described, /Reads: confined to the project/);
  assert.match(described, /metadata/);
});

test('an unsupported platform describes the reason, not a capability', () => {
  const support = sandbox.resolveScriptSupport({ platform: 'win32', paths: paths() });
  const described = sandbox.describeScriptCapabilities(support);
  assert.match(described, /unavailable/);
  assert.doesNotMatch(described, /Isolation:/);
});

// ---------------------------------------------------------------------------
// The profile
// ---------------------------------------------------------------------------

test('the profile denies by default and names only the granted roots', () => {
  const profile = sandbox.buildSeatbeltProfile(
    paths({ skillRoots: ['/private/tmp/cow/skills'] }),
  );
  const denyIndex = profile.indexOf('(deny default)');
  assert.ok(denyIndex >= 0, 'must deny by default');
  assert.ok(profile.indexOf('(allow file-write*') > denyIndex);
  const writeSection = profile.slice(profile.indexOf('(allow file-write*'));
  assert.match(writeSection, /"\/private\/tmp\/cow\/proj"/);
  assert.match(writeSection, /"\/private\/tmp\/cow\/tmp"/);
  // A skill cache is readable but never writable.
  assert.doesNotMatch(writeSection, /skills/);
  // Only the device nodes a subprocess actually needs, and only as literals --
  // a `(subpath "/dev")` would hand over every device on the machine.
  assert.match(writeSection, /\(literal "\/dev\/null"\)/);
  assert.doesNotMatch(writeSection, /\(subpath "\/dev"\)/);
});

test('the profile keeps the allowances the probes proved are required', () => {
  const profile = sandbox.buildSeatbeltProfile(paths());
  for (const needed of [
    '(allow process-exec)',
    '(allow process-fork)',
    '(allow sysctl-read)',
    '(allow mach-lookup)',
    '"/System/Volumes/Preboot"',
    '"/private/var/select"',
    // Without this the interpreter cannot be exec'd at all, because path
    // resolution needs a directory lookup on every ancestor.
    '(allow file-read-metadata (subpath "/"))',
    // `(target self)` alone means the worker cannot kill its own children, so
    // cancellation silently fails and a stopped command keeps running.
    '(allow signal (target children))',
  ]) {
    assert.ok(profile.includes(needed), `missing ${needed}`);
  }
  // The root is a *literal*: enough for dyld, and far less than `subpath "/"`,
  // which would make every file on the machine readable. The metadata allowance
  // is the one place a blanket `(subpath "/")` is correct, so the check is
  // scoped to the `file-read*` section.
  assert.match(profile, /\(literal "\/"\)/);
  const fileReadSection = profile.slice(
    profile.indexOf('(allow file-read*'),
    profile.indexOf('(allow file-write*'),
  );
  assert.equal(fileReadSection.includes('(subpath "/")'), false,
    'a blanket read of "/" would make the read boundary decorative');
});

test('ancestors are traversable without being readable', () => {
  const profile = sandbox.buildSeatbeltProfile(
    paths({ projectRoot: '/private/tmp/cow/proj' }),
  );
  const readSection = profile.slice(
    profile.indexOf('(allow file-read*'),
    profile.indexOf('(allow file-write*'),
  );
  // Traversal of each ancestor...
  for (const ancestor of ['/private', '/private/tmp', '/private/tmp/cow']) {
    assert.ok(readSection.includes(`(literal "${ancestor}")`), `missing ${ancestor}`);
  }
  // ...but not the subtree of any ancestor, which would grant the siblings.
  assert.equal(readSection.includes('(subpath "/private/tmp")'), false);

  // And the ancestors of every runtime path, not only the project's.
  const withRuntime = sandbox.buildSeatbeltProfile(
    paths({ runtimeReadPaths: ['/opt/venv/lib/python3.14/site-packages'] }),
  );
  for (const ancestor of ['/opt', '/opt/venv', '/opt/venv/lib']) {
    assert.ok(withRuntime.includes(`(literal "${ancestor}")`), `missing ${ancestor}`);
  }
  assert.deepEqual(sandbox.ancestorDirectories(['/a/b/c']), ['/a/b', '/a']);
  assert.deepEqual(sandbox.ancestorDirectories(['/']), []);
});

test('the profile grants no network', () => {
  const profile = sandbox.buildSeatbeltProfile(paths());
  assert.doesNotMatch(profile, /network/);
});

test('a path that tries to inject a policy form is escaped, not embedded', () => {
  const hostile = '/private/tmp/co"w)\n(allow file-write* (subpath "/")';
  const escaped = sandbox.escapeSeatbeltPath(hostile);
  // The quote must be escaped; without that the literal ends early and the rest
  // of the name is read as policy. A literal newline is *allowed* inside an SBPL
  // string -- verified against sandbox-exec, which keeps it inside the literal
  // (see the boundary test below) -- so it is not escaped here.
  assert.equal(escaped.includes('\\"'), true);
  // Every quote in the output must be escaped -- one that survives unescaped
  // ends the literal early and lets the rest of the name be read as policy.
  assert.equal(/(?<!\\)"/.test(escaped), false, 'no unescaped quote may survive');
  assert.equal(/(?<!\\)\\"/.test(escaped) || escaped.includes('\\"'), true);
  assert.equal(
    sandbox.escapeSeatbeltPath('a\\"b'),
    'a\\\\\\"b',
    'a backslash must be escaped before the quote it precedes',
  );
});

test('the interpreter installation is readable but never writable', () => {
  const profile = sandbox.buildSeatbeltProfile(
    paths({ runtimeReadPaths: ['/Applications/Cow.app/python'] }),
  );
  const writeSection = profile.slice(profile.indexOf('(allow file-write*'));
  assert.doesNotMatch(writeSection, /python/, 'the runtime must not be writable');
  assert.match(profile, /Applications\/Cow\.app\/python/);
  // Without this the interpreter cannot load its own stdlib and dies before the
  // handshake -- which reads as "the worker crashed".
  assert.match(profile, /\(allow file-read\*/);
});

test('escapeSeatbeltPath escapes backslashes before quotes', () => {
  assert.equal(sandbox.escapeSeatbeltPath('a\\b'), 'a\\\\b');
  assert.equal(sandbox.escapeSeatbeltPath('a"b'), 'a\\"b');
  assert.equal(sandbox.escapeSeatbeltPath('a\\"b'), 'a\\\\\\"b');
});

// ---------------------------------------------------------------------------
// The launch plan
// ---------------------------------------------------------------------------

test('the plan launches the worker through the sandbox, never directly', () => {
  const plan = sandbox.buildLaunchPlan({
    platform: 'darwin',
    paths: paths(),
    pythonPath: '/app/python3',
    seatbeltAvailable: true,
    workingDirectory: '/app',
    env: { PATH: '/usr/bin' },
  });
  assert.equal(plan.ok, true);
  assert.equal(plan.command, '/usr/bin/sandbox-exec');
  assert.equal(plan.args[0], '-p');
  assert.equal(plan.args[1], plan.sandbox.profile);
  assert.equal(plan.args[2], '/app/python3');
  assert.deepEqual(plan.args.slice(3), ['-m', 'agent.desktop_local.worker']);
  assert.equal(plan.sandbox.kind, 'seatbelt');
});

test('the plan refuses on a platform without a probed boundary', () => {
  const plan = sandbox.buildLaunchPlan({
    platform: 'win32',
    paths: paths(),
    pythonPath: 'C:\\app\\python.exe',
    workingDirectory: 'C:\\app',
    env: {},
  });
  assert.equal(plan.ok, false);
  assert.equal(plan.code, 'unsupported_platform');
  // No command/args at all: a caller cannot accidentally use a partial plan.
  assert.equal(plan.command, undefined);
  assert.equal(plan.args, undefined);
});

// ---------------------------------------------------------------------------
// Environment scrubbing
// ---------------------------------------------------------------------------

test('only what a tool needs is kept', () => {
  const clean = env.scrubWorkerEnv({
    PATH: '/usr/bin',
    HOME: '/Users/me',
    OTHER_THING: 'x',
    COW_DESKTOP: '1',
  });
  assert.equal(clean.PATH, '/usr/bin');
  assert.equal(clean.HOME, '/Users/me');
  assert.equal(clean.COW_DESKTOP, '1');
  assert.equal(clean.OTHER_THING, undefined);
});

test('credentials never survive, whatever their name looks like', () => {
  const clean = env.scrubWorkerEnv({
    PATH: '/usr/bin',
    COW_SESSION_TOKEN: 'bearer',
    OPENAI_API_KEY: 'sk-1',
    CUSTOM_api_key: 'sk-2',
    DESKTOP_AUTH_SECRET: 's',
    IDENTITY_DB_PASSWORD: 'p',
    MY_COOKIE: 'c',
    PATH_CREDENTIAL_FILE: '/etc/shadow',
    COW_IDENTITY_DB: '/data/identity.db',
  });
  assert.deepEqual(Object.keys(clean), ['PATH']);
});

test('the launcher cannot smuggle a secret in through its own additions', () => {
  const clean = env.scrubWorkerEnv({ PATH: '/usr/bin' }, {
    extra: {
      COW_DESKTOP_WORKER_ROOT: '/private/tmp/cow/proj',
      COW_DESKTOP_TOKEN: 'launch-token',
    },
  });
  assert.equal(clean.COW_DESKTOP_WORKER_ROOT, '/private/tmp/cow/proj');
  assert.equal(clean.COW_DESKTOP_TOKEN, undefined);
});

test('the temp directory is set from the grant, never inherited', () => {
  // Inherited TMPDIR points outside the sandbox's writable grant, where
  // `tempfile` finds nothing usable -- so the temp variables are overwritten
  // rather than kept, in every name a tool might consult.
  const clean = env.scrubWorkerEnv(
    {
      PATH: '/usr/bin',
      TMPDIR: '/var/folders/unwritable/T',
      TEMP: '/var/folders/unwritable/T',
      TMP: '/var/folders/unwritable/T',
    },
    { tempRoot: '/private/tmp/cow/run' },
  );
  assert.equal(clean.TMPDIR, '/private/tmp/cow/run');
  assert.equal(clean.TEMP, '/private/tmp/cow/run');
  assert.equal(clean.TMP, '/private/tmp/cow/run');
});

test('a worker with no granted temp directory is not given a fake one', () => {
  // Setting TMPDIR to something unwritable would be worse than leaving it out:
  // the failure would move from a clear "no usable temp directory" to a
  // confusing EPERM inside whichever tool touched it.
  const clean = env.scrubWorkerEnv({ PATH: '/usr/bin', TMPDIR: '/var/folders/x/T' });
  assert.deepEqual(Object.keys(clean), ['PATH']);
});

// ---------------------------------------------------------------------------
// Framing
// ---------------------------------------------------------------------------

test('a frame needs an id and an op', () => {
  assert.equal(sessionMod.decodeFrame('{"id":"a","op":"ping"}').ok, true);
  for (const bad of ['', '   ', 'not json', '[1]', '{"op":"ping"}', '{"id":"","op":"ping"}',
    '{"id":"a"}', '{"id":1,"op":"ping"}']) {
    const decoded = sessionMod.decodeFrame(bad);
    assert.equal(decoded.ok, false, `accepted ${JSON.stringify(bad)}`);
    assert.equal(decoded.error.code, 'invalid_request');
  }
});

test('an oversized frame is refused by size, before parsing', () => {
  const decoded = sessionMod.decodeFrame('x'.repeat(sessionMod.MAX_FRAME_BYTES + 1));
  assert.equal(decoded.ok, false);
  assert.equal(decoded.error.code, 'frame_too_large');
});

test('encodeFrame emits exactly one line', () => {
  const line = sessionMod.encodeFrame({ id: 'a', op: 'ping' });
  assert.equal(line.endsWith('\n'), true);
  assert.equal(line.split('\n').length, 2);
});

test('a reply is validated as a reply, not as a request', () => {
  // The worker answers with `status`, never with `op` (encode_reply in
  // agent/desktop_local/protocol.py). Validating answers with the request rule
  // rejects every one of them, and the caller then sees a timeout for a call
  // that actually succeeded -- the worst possible failure mode.
  const success = sessionMod.decodeReply('{"id":"a","status":"success","root":"/x"}');
  assert.equal(success.ok, true, JSON.stringify(success));
  const failure = sessionMod.decodeReply('{"id":"a","status":"error","error":{"code":"internal"}}');
  assert.equal(failure.ok, true, JSON.stringify(failure));

  for (const bad of ['', '   ', 'not json', '[1]', '{"status":"success"}',
    '{"id":"","status":"success"}', '{"id":"a"}', '{"id":"a","status":"maybe"}',
    '{"id":"a","op":"hello"}']) {
    const decoded = sessionMod.decodeReply(bad);
    assert.equal(decoded.ok, false, `accepted ${JSON.stringify(bad)}`);
  }
});

test('a log line on the wire is not mistaken for a reply', () => {
  // The worker writes its own warnings to stdout before the handshake; those
  // must be dropped, not treated as an unparseable reply.
  assert.equal(
    sessionMod.decodeReply('[WARNING][2026-09-30][log.py:0] - file logging disabled').ok,
    false,
  );
  assert.equal(sessionMod.decodeReply(Buffer.from('{"id":"a","status":"success"}\n')).ok, true);
});

// ---------------------------------------------------------------------------
// The interpreter, and the launch seam that consumes it
// ---------------------------------------------------------------------------

test('the interpreter is found where the backend keeps it, or not at all', () => {
  const backend = '/app/backend';
  assert.equal(
    interpreterMod.findInterpreter(backend, (p) => p === path.join(backend, '.venv', 'bin', 'python')),
    path.join(backend, '.venv', 'bin', 'python'),
  );
  assert.equal(
    interpreterMod.findInterpreter(backend, (p) => p === path.join(backend, '.venv', 'Scripts', 'python.exe')),
    path.join(backend, '.venv', 'Scripts', 'python.exe'),
  );
  // No bare `python3` fallback: a PATH lookup picks up an interpreter whose
  // dependencies this app never validated.
  assert.equal(interpreterMod.findInterpreter(backend, () => false), null);
});

test('an installed app runs the worker from its own bundle, not a virtualenv', () => {
  const backend = '/Applications/容大AI.app/Contents/Resources/backend';
  const bundled = path.join(backend, 'cowagent-backend', 'cowagent-backend');
  // The shipped layout: byte-for-byte the rule python-manager.ts uses to start
  // the HTTP backend, so "the app runs" and "the app can run scripts" cannot
  // disagree about where the bundle is.
  assert.equal(
    interpreterMod.findWorkerRuntime(backend, 'darwin', (p) => p === bundled).path,
    bundled,
  );
  assert.equal(
    interpreterMod.findWorkerRuntime(
      backend, 'darwin', (p) => p === path.join(backend, 'cowagent-backend', 'cowagent-backend'),
    ).frozen,
    true,
  );
  assert.deepEqual(
    interpreterMod.findWorkerRuntime(backend, 'darwin', (p) => p === bundled).args,
    [interpreterMod.WORKER_FLAG],
  );
  // Windows names the executable differently, and the flat layout is the
  // fallback for an install whose bundle directory was moved up one level.
  const flat = path.join(backend, 'cowagent-backend.exe');
  assert.equal(interpreterMod.findWorkerRuntime(backend, 'win32', (p) => p === flat).path, flat);
  // A source checkout has no bundle, so it still resolves the interpreter.
  assert.equal(
    interpreterMod.findWorkerRuntime(
      '/repo/desktop', 'darwin', (p) => p === path.join('/repo/desktop', '.venv', 'bin', 'python'),
    ).args.join(' '),
    `-m ${interpreterMod.WORKER_MODULE}`,
  );
  // Nothing at all is still a refusal, not a PATH guess.
  assert.equal(interpreterMod.findWorkerRuntime('/repo/desktop', 'darwin', () => false), null);
});

test('a frozen probe asks the bundle itself, and reads only measured paths', () => {
  const backend = '/Applications/Cow.app/Contents/Resources/backend';
  const bundled = path.join(backend, 'cowagent-backend', 'cowagent-backend');
  const seen = [];
  const probe = interpreterMod.probeInterpreter(
    interpreterMod.findWorkerRuntime(backend, 'darwin', (p) => p === bundled),
    (command) => {
      seen.push(command);
      return JSON.stringify({
        realPath: bundled,
        // A frozen build can still report the machine it was built on. Those
        // paths must not become read allowances: on the build machine they exist
        // and widen the sandbox for no reason, and on a user's machine they do
        // nothing at all.
        basePrefix: '/build/agent/python',
        stdlib: '/build/agent/python/lib/python3.11',
        purelib: '/build/agent/python/lib/python3.11/site-packages',
        frozen: true,
        bundleRoot: path.join(backend, 'cowagent-backend', '_internal'),
      });
    },
  );
  // `-c` is not a thing a frozen executable can be asked for.
  assert.deepEqual(seen[0], [bundled, interpreterMod.WORKER_FLAG, interpreterMod.WORKER_PROBE_FLAG]);
  assert.equal(probe.frozen, true);
  const reads = interpreterMod.interpreterReadPaths(probe);
  const internal = path.join(backend, 'cowagent-backend', '_internal');
  assert.ok(reads.includes(internal), `missing ${internal} in ${JSON.stringify(reads)}`);
  assert.ok(reads.includes(path.join(backend, 'cowagent-backend')), JSON.stringify(reads));
  for (const ignored of ['/build/agent/python', '/build/agent/python/lib/python3.11']) {
    assert.equal(reads.includes(ignored), false, `${ignored} must not be granted`);
  }
});

test('a virtualenv probe reports the installation, not the symlink', () => {
  const probe = interpreterMod.probeInterpreter('/app/backend/.venv/bin/python', () =>
    JSON.stringify({
      realPath: '/usr/local/python/bin/python3.14',
      basePrefix: '/usr/local/python',
      stdlib: '/usr/local/python/lib/python3.14',
      purelib: '/app/backend/.venv/lib/python3.14/site-packages',
    }) + '\n');
  assert.equal(probe.realPath, '/usr/local/python/bin/python3.14');
  const reads = interpreterMod.interpreterReadPaths(probe);
  // All four matter: the symlink target's directory is not under the base
  // prefix, and the venv's site-packages is not under either.
  for (const needed of [
    '/usr/local/python',
    '/usr/local/python/lib/python3.14',
    '/app/backend/.venv/lib/python3.14/site-packages',
    '/usr/local/python/bin',
  ]) {
    assert.ok(reads.includes(needed), `missing ${needed} in ${JSON.stringify(reads)}`);
  }
  assert.equal(reads.includes('/'), false, '"/" is granted wholesale already');
});

test('a warning line before the probe JSON does not defeat the probe', () => {
  const probe = interpreterMod.probeInterpreter('/app/python3', () =>
    '[WARNING] something\n{"realPath":"/app/python3","basePrefix":"/app"}\n');
  assert.equal(probe.basePrefix, '/app');
});

test('an interpreter that cannot answer is a failure, not a guess', () => {
  for (const run of [
    () => { throw new Error('no such file'); },
    () => 'not json',
    () => '{"basePrefix":"/app"}',
  ]) {
    assert.equal(interpreterMod.probeInterpreter('/app/python3', run), null);
  }
});

test('a broken interpreter is not re-probed on every call', () => {
  let calls = 0;
  const registry = new interpreterMod.InterpreterRegistry(() => {
    calls += 1;
    throw new Error('broken');
  });
  assert.equal(registry.get('/app/python3'), null);
  assert.equal(registry.get('/app/python3'), null);
  assert.equal(calls, 1, 'a remembered failure must not become a stall');
  registry.forget('/app/python3');
  assert.equal(registry.get('/app/python3'), null);
  assert.equal(calls, 2);
});

test('a grant without an interpreter is refused, not run unconfined', () => {
  const plan = launchMod.buildGrantLaunchPlan({
    platform: 'darwin',
    grant: { projectRoot: '/private/tmp/cow/proj', tempRoot: '/private/tmp/cow/tmp' },
    interpreter: null,
    seatbeltAvailable: true,
    backendPath: '/app/backend',
    baseEnv: {},
  });
  assert.equal(plan.ok, false);
  assert.equal(plan.code, 'python_not_found');
});

test('the production seam names everything the worker needs to start', () => {
  const probe = {
    pythonPath: '/app/backend/.venv/bin/python',
    realPath: '/usr/local/python/bin/python3.14',
    basePrefix: '/usr/local/python',
    stdlib: '/usr/local/python/lib/python3.14',
    purelib: '/app/backend/.venv/lib/python3.14/site-packages',
  };
  const plan = launchMod.buildGrantLaunchPlan({
    platform: 'darwin',
    grant: {
      projectRoot: '/private/tmp/cow/proj',
      tempRoot: '/private/tmp/cow/tmp',
      skillRoots: ['/private/tmp/cow/skills'],
    },
    interpreter: probe,
    seatbeltAvailable: true,
    backendPath: '/app/backend',
    baseEnv: { PATH: '/usr/bin' },
  });
  assert.equal(plan.ok, true, JSON.stringify(plan));
  const reads = plan.capabilities.readable;
  assert.ok(reads.includes('/private/tmp/cow/proj'));
  assert.ok(reads.includes('/private/tmp/cow/skills'));
  // The interpreter installation...
  assert.ok(reads.includes('/usr/local/python'));
  // ...and the directory the `-m agent.desktop_local.worker` module lives in.
  assert.ok(reads.includes('/app/backend'));
  // None of which may become writable.
  assert.deepEqual(plan.capabilities.writable, ['/private/tmp/cow/proj', '/private/tmp/cow/tmp']);
});

test('an installed app is confined with its bundle argv, not a module flag', () => {
  const bundled = '/Applications/Cow.app/Contents/Resources/backend/cowagent-backend/cowagent-backend';
  const internal = '/Applications/Cow.app/Contents/Resources/backend/cowagent-backend/_internal';
  const plan = launchMod.buildGrantLaunchPlan({
    platform: 'darwin',
    grant: { projectRoot: '/private/tmp/cow/proj', tempRoot: '/private/tmp/cow/tmp' },
    interpreter: {
      pythonPath: bundled,
      realPath: bundled,
      basePrefix: '/build/agent/python',
      stdlib: '/build/agent/python/lib/python3.11',
      purelib: '/build/agent/python/lib/python3.11/site-packages',
      runtime: { path: bundled, args: ['--desktop-local-worker'], frozen: true },
      frozen: true,
      bundleRoot: internal,
    },
    seatbeltAvailable: true,
    backendPath: '/Applications/Cow.app/Contents/Resources/backend',
    baseEnv: {},
  });
  assert.equal(plan.ok, true, JSON.stringify(plan));
  // `-m` would be read by the bundle as its own argument, so the argv has to be
  // the flag -- and the bundle has to be what the profile allows to be read.
  assert.deepEqual(plan.args.slice(-2), [bundled, '--desktop-local-worker']);
  assert.ok(plan.capabilities.readable.includes(internal));
  assert.ok(plan.capabilities.readable.includes(
    '/Applications/Cow.app/Contents/Resources/backend/cowagent-backend',
  ));
  // The build machine's Python must not travel into a user's sandbox.
  assert.equal(plan.capabilities.readable.includes('/build/agent/python'), false);
});

// ---------------------------------------------------------------------------
// The boundary, for real
// ---------------------------------------------------------------------------

function pythonFor(repoRoot) {
  const venv = path.join(repoRoot, '.venv', 'bin', 'python');
  return fs.existsSync(venv) ? venv : process.env.PYTHON || 'python3';
}

/**
 * Which runtime the boundary is exercised against.
 *
 * Both shapes are real and they are **not** interchangeable, which is why this
 * is selectable rather than fixed to the checkout:
 *
 *   * ``source`` (default) -- the developer's virtualenv, running
 *     ``python -m agent.desktop_local.worker``. This is what the suite has always
 *     asserted, and it is the only shape available without a build.
 *   * ``installed`` -- a **packaged** backend (task 4.9's "随包 Python" leg and
 *     the local half of 10.2). ``COW_A26_BACKEND`` names a ``resources/backend``
 *     directory, the resolver picks the frozen onedir executable exactly the way
 *     the shipping app does, and ``--probe`` supplies the read allowances.
 *
 * The distinction is not cosmetic: a frozen build has no ``.venv``, its
 * ``base_prefix``/``stdlib`` name the *build* machine, and ``interpreter.ts``
 * deliberately grants it a narrower read set (only ``bundleRoot``). A profile
 * that runs the worker from a checkout can therefore fail, or be wider than the
 * one a user gets, once frozen -- so the boundary has to be re-asserted on the
 * installed shape, not inferred from the checkout's.
 */
function runtimeFor(repoRoot) {
  const packaged = process.env.COW_A26_BACKEND;
  if (!packaged) {
    return { backendPath: repoRoot, runtime: pythonFor(repoRoot) };
  }
  const resolved = fs.realpathSync(packaged);
  const runtime = interpreterMod.findWorkerRuntime(resolved);
  assert.ok(
    runtime && runtime.frozen,
    `COW_A26_BACKEND=${packaged} has no frozen worker; expected `
      + `${path.join(resolved, 'cowagent-backend', 'cowagent-backend')}`,
  );
  return { backendPath: resolved, runtime };
}

/** True when the run is against an installed bundle rather than a checkout. */
function isInstalled() {
  return Boolean(process.env.COW_A26_BACKEND);
}

/**
 * The interpreter the *escape* probes are run with.
 *
 * These probes ask the operating system, not the worker, to violate the boundary
 * ("open this dynamically built path", "read through this symlink", "spawn a
 * grandchild and let it try"), so the claim is about the sandbox and any
 * interpreter that can start inside it proves the same thing.
 *
 * A checkout uses its virtualenv. An **installed** run does not, and cannot: the
 * frozen profile grants only `bundleRoot`, deliberately, so no interpreter
 * outside the app is readable -- and on macOS 26 `/usr/bin/python3` is not an
 * interpreter at all but an `xcrun` shim whose dylib lives under
 * `/Library/Developer/CommandLineTools`, which the same profile blocks:
 *
 *   xcrun: error: unable to load libxcrun (dlopen(...): file system sandbox blocked open())
 *
 * That is the sandbox working, not a defect -- but it means the interpreter-based
 * escapes are a checkout-only claim once installed, and the installed leg makes
 * the kernel claim through the worker's own `bash` tool instead (a shell command
 * is not path-checked by the worker, so anything the kernel allows, happens).
 * `COW_A26_PROBE_PYTHON` can name an interpreter that does start inside an
 * installed sandbox, for a platform where one exists.
 */
function probePythonFor(repoRoot) {
  if (!isInstalled()) return pythonFor(repoRoot);
  const named = process.env.COW_A26_PROBE_PYTHON;
  return named && fs.existsSync(named) ? named : null;
}

/**
 * Build the plan through the *production* seam, so the interpreter's read
 * allowances come from `interpreter.ts` -- the same code the app runs. Working
 * them out here instead would let the app ship broken while this suite is green.
 */
function planFor(repoRoot, proj, run, options = {}) {
  const { backendPath, runtime } = runtimeFor(repoRoot);
  const registry = new interpreterMod.InterpreterRegistry();
  const plan = launchMod.buildGrantLaunchPlan({
    platform: process.platform,
    grant: { projectRoot: proj, tempRoot: run, skillRoots: options.skillRoots },
    interpreter: registry.get(runtime),
    seatbeltAvailable: true,
    backendPath,
    // The seam scrubs and sets TMPDIR itself, so the test cannot accidentally
    // prove that a hand-scrubbed environment works while the app's does not.
    baseEnv: options.env ?? process.env,
  });
  assert.equal(plan.ok, true, JSON.stringify(plan));
  return plan;
}

const onMac = process.platform === 'darwin';
const seatbelt = fs.existsSync('/usr/bin/sandbox-exec');
const sandboxed = {
  skip: !(onMac && seatbelt) || !probePythonFor(root)
    ? (onMac && seatbelt ? 'no system interpreter for escape probes' : 'needs macOS + sandbox-exec')
    : false,
};

/**
 * Run one real sandboxed worker against a throwaway project.
 *
 * Every A26/A27/A17 assertion below needs a live process, so the setup is
 * shared: create the tree, optionally stage files, launch through the production
 * seam, and clean up the process tree whatever the body does.
 */
async function withWorker(options, body) {
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-a-')));
  const proj = path.join(tmp, 'proj');
  const run = path.join(tmp, 'run');
  fs.mkdirSync(proj, { recursive: true });
  fs.mkdirSync(run, { recursive: true });
  const staged = options.setup ? options.setup({ tmp, proj, run }) ?? {} : {};
  const plan = planFor(root, proj, run, { ...options, ...staged });
  const session = new sessionMod.WorkerSession(
    plan, {}, { callTimeoutMs: options.callTimeoutMs ?? 60000 },
  );
  try {
    const hello = await session.hello(proj, run);
    assert.equal(hello.status, 'success', JSON.stringify(hello));
    await body({ session, hello, tmp, proj, run, pythonPath: probePythonFor(root), ...staged });
  } finally {
    await session.stop();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

/** The `bash` tool's own result shape, which is what the model would see. */
function bashOut(reply) {
  const result = reply.result ?? {};
  return {
    status: reply.status,
    output: String(result.output ?? ''),
    exitCode: result.exit_code,
    details: result.details ?? null,
  };
}

/** Refused by the kernel, as opposed to refused by a substring filter. */
const KERNEL_DENIAL = /Operation not permitted/;

// ---------------------------------------------------------------------------
// A26: the boundary is the OS's, not a string check
// ---------------------------------------------------------------------------

test('A26: Python os.open on a dynamically built outside path is refused', sandboxed, async () => {
  await withWorker({}, async ({ session, tmp, proj, pythonPath }) => {
    fs.writeFileSync(path.join(tmp, 'secret.txt'), 'do not read\n');
    await session.call('write', { path: 'probe.py', content: [
      'import os, sys',
      // Built at runtime from parts: no literal of the target path appears in
      // the command, so only the OS boundary can be what stops it.
      'target = "/" + "/".join(',
      '    p for p in os.path.join(sys.argv[1], "secret.txt").split("/") if p)',
      'try:',
      '    fd = os.open(target, os.O_RDONLY)',
      '    print("READ_OK", os.read(fd, 4096).decode("utf-8", "replace").strip())',
      'except OSError as exc:',
      '    print("DENIED", exc.errno, exc.strerror)',
    ].join('\n') });

    const out = bashOut(await session.call('bash', {
      command: `${JSON.stringify(pythonPath)} probe.py ${JSON.stringify(tmp)}`,
    }));
    assert.equal(out.output.includes('do not read'), false, `contents leaked: ${out.output}`);
    assert.match(out.output, /DENIED/);
    assert.match(out.output, KERNEL_DENIAL);
  });
});

test('A26: a symlink inside the project does not become a way out', sandboxed, async () => {
  await withWorker({}, async ({ session, tmp, proj, pythonPath }) => {
    fs.writeFileSync(path.join(tmp, 'secret.txt'), 'do not read\n');
    // A link the *user's own project* contains: the resolver follows it, which
    // is exactly the escape a string filter on the command would miss.
    fs.symlinkSync(path.join(tmp, 'secret.txt'), path.join(proj, 'link.txt'));

    const viaShell = bashOut(await session.call('bash', { command: 'cat link.txt' }));
    assert.equal(viaShell.output.includes('do not read'), false, 'shell followed the link out');
    assert.match(viaShell.output, KERNEL_DENIAL);

    await session.call('write', { path: 'probe.py', content: [
      'try:',
      '    print("READ_OK", open("link.txt").read().strip())',
      'except OSError as exc:',
      '    print("DENIED", exc.errno, exc.strerror)',
    ].join('\n') });
    const viaPython = bashOut(await session.call('bash', {
      command: `${JSON.stringify(pythonPath)} probe.py`,
    }));
    assert.equal(viaPython.output.includes('do not read'), false, 'python followed the link out');
    assert.match(viaPython.output, KERNEL_DENIAL);
  });
});

test('A26: a grandchild subprocess inherits the same boundary', sandboxed, async () => {
  await withWorker({}, async ({ session, tmp, proj, pythonPath }) => {
    fs.writeFileSync(path.join(tmp, 'secret.txt'), 'do not read\n');
    await session.call('write', { path: 'probe.py', content: [
      'import subprocess, sys',
      'proc = subprocess.run(["/bin/cat", sys.argv[1]], capture_output=True, text=True)',
      'print("rc", proc.returncode)',
      'print("out", proc.stdout.strip())',
      'print("err", proc.stderr.strip())',
    ].join('\n') });

    const out = bashOut(await session.call('bash', {
      command: `${JSON.stringify(pythonPath)} probe.py ${JSON.stringify(path.join(tmp, 'secret.txt'))}`,
    }));
    // The denial has to reach the *grandchild*: granting the parent alone would
    // let one `subprocess.run` read anything.
    assert.equal(out.output.includes('do not read'), false, `child leaked: ${out.output}`);
    assert.match(out.output, KERNEL_DENIAL);
  });
});

test('A26: project, temp directory and runtime still work normally', sandboxed, async () => {
  await withWorker({}, async ({ session, proj, run, pythonPath }) => {
    await session.call('write', { path: 'probe.py', content: [
      'import json, hashlib, sys',
      // A stdlib import proves the interpreter's own installation is readable;
      // without it the worker dies before the handshake and every call looks
      // like a crash.
      'print("STDLIB_OK", hashlib.sha256(b"x").hexdigest()[:8], json.dumps({"a": 1}))',
      'print("ARGV", sys.argv[1])',
    ].join('\n') });

    const runtime = bashOut(await session.call('bash', {
      command: `${JSON.stringify(pythonPath)} probe.py ${JSON.stringify(run)}`,
    }));
    assert.equal(runtime.status, 'success', JSON.stringify(runtime));
    assert.match(runtime.output, /STDLIB_OK/);

    const projectWrite = bashOut(await session.call('bash', { command: 'echo made > shell.txt' }));
    assert.equal(projectWrite.status, 'success', JSON.stringify(projectWrite));
    assert.equal(fs.readFileSync(path.join(proj, 'shell.txt'), 'utf8').trim(), 'made');

    // The run's temp directory is writable, but is not the project.
    const tempWrite = bashOut(await session.call('bash', {
      command: `echo scratch > ${JSON.stringify(path.join(run, 'scratch.txt'))}`,
    }));
    assert.equal(tempWrite.status, 'success', JSON.stringify(tempWrite));
    assert.equal(fs.readFileSync(path.join(run, 'scratch.txt'), 'utf8').trim(), 'scratch');
  });
});

test('A26: a temp file can actually be created inside the run', sandboxed, async () => {
  await withWorker({}, async ({ session, run, pythonPath }) => {
    await session.call('write', { path: 'probe.py', content: [
      'import os, tempfile',
      'print("TMPDIR", os.environ.get("TMPDIR", ""))',
      'print("GETTEMPDIR", tempfile.gettempdir())',
      // The real assertion: creating one, not just naming the directory.
      'with tempfile.NamedTemporaryFile(delete=False) as handle:',
      '    handle.write(b"scratch")',
      '    print("CREATED", handle.name)',
    ].join('\n') });
    const out = bashOut(await session.call('bash', {
      command: `${JSON.stringify(pythonPath)} probe.py`,
    }));
    // Inheriting the parent's TMPDIR points outside the writable grant, and
    // `tempfile` then reports "No usable temporary directory found" -- which
    // breaks the bash tool itself as soon as output exceeds its inline limit.
    assert.equal(out.status, 'success', JSON.stringify(out));
    assert.doesNotMatch(out.output, /No usable temporary directory/);
    assert.match(out.output, /CREATED/);
    assert.match(out.output, new RegExp(`GETTEMPDIR ${run.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`));
  });

  // The worker's own tooling needs it too: a bash call whose output exceeds the
  // inline limit spills the full output to a temp file.
  await withWorker({}, async ({ session, pythonPath }) => {
    const out = bashOut(await session.call('bash', {
      command: `${JSON.stringify(pythonPath)} -c "print('x' * 200000)"`,
    }));
    assert.equal(out.exitCode, 0, JSON.stringify(out));
    assert.equal(out.details?.truncation?.truncated, true, JSON.stringify(out.details));
  });
});

// ---------------------------------------------------------------------------
// A27: secrets, caches and the network
// ---------------------------------------------------------------------------

test('A27: no secret from the launcher environment reaches the worker', sandboxed, async () => {
  const planted = {
    COW_DESKTOP_TOKEN: 'launch-secret',
    COW_IDENTITY_DB: '/data/identity.db',
    OPENAI_API_KEY: 'sk-should-not-travel',
    COW_SESSION_TOKEN: 'bearer-should-not-travel',
  };
  await withWorker({ env: { ...process.env, ...planted } }, async ({ session }) => {
    const out = bashOut(await session.call('bash', { command: 'env' }));
    const text = out.output;
    for (const [name, value] of Object.entries(planted)) {
      assert.equal(text.includes(value), false, `${name} reached the worker`);
    }
    // The scrub is a whitelist, not a blacklist: the worker does not get a
    // partially-populated environment that a tool might read.
    assert.equal(/API_KEY|_TOKEN=|_SECRET|IDENTITY_DB/.test(text), false, text.slice(0, 400));
  });
});

test('A27: identity data and main-process config outside the project are unreadable',
  sandboxed, async () => {
    await withWorker({}, async ({ session, tmp }) => {
      // Named like the things the spec calls out, in a directory the run has no
      // business reading.
      const vault = path.join(tmp, 'vault');
      fs.mkdirSync(vault);
      fs.writeFileSync(path.join(vault, 'identity.db'), 'BCRYPT-HASH\n');
      fs.writeFileSync(path.join(vault, 'config.json'), '{"desktop_token": "xyz"}\n');

      for (const name of ['identity.db', 'config.json']) {
        const out = bashOut(await session.call('bash', {
          command: `cat ${JSON.stringify(path.join(vault, name))}`,
        }));
        assert.equal(out.output.includes('BCRYPT-HASH') || out.output.includes('desktop_token'),
          false, `${name} was readable: ${out.output}`);
        assert.match(out.output, KERNEL_DENIAL, `${name} was not refused by the kernel`);
      }
    });
  });

test('A27: a skill cache is readable but cannot be modified', sandboxed, async () => {
  await withWorker({
    setup: ({ tmp }) => {
      const skills = path.join(tmp, 'skills');
      fs.mkdirSync(skills, { recursive: true });
      fs.writeFileSync(path.join(skills, 'helper.py'), 'VALUE = 1\n');
      return { skillRoots: [skills] };
    },
  }, async ({ session, skillRoots }) => {
    const skills = skillRoots[0];
    const read = bashOut(await session.call('bash', {
      command: `cat ${JSON.stringify(path.join(skills, 'helper.py'))}`,
    }));
    assert.equal(read.status, 'success', JSON.stringify(read));
    assert.match(read.output, /VALUE = 1/);

    // Read-only means read-only: a cache a script could edit is a cache the next
    // run cannot trust.
    for (const command of [
      `echo pwned > ${JSON.stringify(path.join(skills, 'helper.py'))}`,
      `echo pwned > ${JSON.stringify(path.join(skills, 'new.py'))}`,
      `rm -f ${JSON.stringify(path.join(skills, 'helper.py'))}`,
    ]) {
      const out = bashOut(await session.call('bash', { command }));
      assert.notEqual(out.exitCode, 0, `skill cache write was allowed: ${command}`);
      assert.match(out.output, KERNEL_DENIAL);
    }
    assert.equal(fs.readFileSync(path.join(skills, 'helper.py'), 'utf8'), 'VALUE = 1\n');
    assert.equal(fs.existsSync(path.join(skills, 'new.py')), false);
  });
});

test('A27: the network is unreachable from inside the sandbox', sandboxed, async () => {
  const net = require('node:net');
  const server = net.createServer((socket) => socket.end('pong'));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  try {
    await withWorker({}, async ({ session, pythonPath }) => {
      await session.call('write', { path: 'probe.py', content: [
        'import socket, sys',
        'sock = socket.socket()',
        'sock.settimeout(5)',
        'try:',
        '    sock.connect(("127.0.0.1", int(sys.argv[1])))',
        '    print("CONNECTED")',
        'except OSError as exc:',
        '    print("DENIED", exc.errno, exc.strerror)',
      ].join('\n') });
      const out = bashOut(await session.call('bash', {
        command: `${JSON.stringify(pythonPath)} probe.py ${port}`,
      }));
      // A live listener on loopback is the strongest available probe: if the
      // sandbox let it through, this would print CONNECTED.
      assert.equal(out.output.includes('CONNECTED'), false, `network reachable: ${out.output}`);
      assert.match(out.output, /DENIED/);
    });
  } finally {
    server.close();
  }
});

// ---------------------------------------------------------------------------
// A17: exits, budgets, drains and the process tree
// ---------------------------------------------------------------------------

test('A17: a non-zero exit is reported as the real exit code', sandboxed, async () => {
  await withWorker({}, async ({ session }) => {
    const out = bashOut(await session.call('bash', { command: 'echo boom >&2; exit 3' }));
    assert.equal(out.exitCode, 3, JSON.stringify(out));
    assert.match(out.output, /boom/);
    // Reported as the tool's own failure, not as a broken worker.
    assert.equal(sessionMod.classifyReply(
      await session.call('bash', { command: 'exit 4' }),
    ).kind, 'tool_error');
  });
});

test('A17: a large drain is truncated with a stated reason', sandboxed, async () => {
  await withWorker({}, async ({ session, pythonPath }) => {
    // ~200 KB, comfortably past the 50 KB / 2000 line tail limit.
    const out = bashOut(await session.call('bash', {
      command: `${JSON.stringify(pythonPath)} -c "print('x' * 200000)"`,
    }));
    assert.equal(out.exitCode, 0, JSON.stringify(out));
    assert.ok(out.details?.truncation, `no truncation reported: ${JSON.stringify(out.details)}`);
    assert.equal(out.details.truncation.truncated, true);
    // The model must be able to tell it is not reading the whole picture.
    assert.ok(out.details.truncation.total_bytes > out.details.truncation.max_bytes);
    assert.ok(out.output.length <= 200000, 'the drain must actually be bounded');
  });
});

test('A17: a background job started by a run does not outlive it', sandboxed, async () => {
  let jobPid = 0;
  await withWorker({}, async ({ session, proj }) => {
    // `run_in_background` is a *tool* feature that deliberately outlives the
    // serial call (see background.py). For a confined local run the run owns its
    // whole tree, so the job must go with it -- a server left running after the
    // project closed is a leak, not a feature.
    const reply = await session.call('bash', {
      command: 'echo $$ > job.pid; exec sleep 120',
      run_in_background: true,
    });
    const outcome = sessionMod.classifyReply(reply);
    assert.equal(outcome.kind, 'tool_result', JSON.stringify(reply));
    assert.match(JSON.stringify(outcome.result), /bash_id/);

    await new Promise((resolve) => setTimeout(resolve, 600));
    jobPid = Number(fs.readFileSync(path.join(proj, 'job.pid'), 'utf8').trim());
    assert.ok(jobPid > 1, String(jobPid));
    assert.equal(isAlive(jobPid), true, 'the job should be running while the run is alive');
  });
  assert.equal(await waitForGone(jobPid), true, `the background job ${jobPid} outlived the run`);
});

/** Poll until the process is gone, so a slow sweep is not read as a leak. */
async function waitForGone(pid, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!isAlive(pid)) return true;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  return !isAlive(pid);
}

test('A17: a backgrounded child does not outlive the run', sandboxed, async () => {
  let pid = 0;
  await withWorker({}, async ({ session }) => {
    const out = bashOut(await session.call('bash', {
      command: 'sleep 137 & echo "bg=$!"',
    }));
    const match = /bg=(\d+)/.exec(out.output);
    assert.ok(match, `no background pid reported: ${out.output}`);
    pid = Number(match[1]);
    assert.equal(isAlive(pid), true, 'the child should be running while the run is alive');
  });
  // `withWorker` has now stopped the session. A worker that exits cleanly and
  // leaves its grandchildren behind is the failure this asserts against.
  assert.equal(await waitForGone(pid), true, `leaked background process ${pid}`);
});

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

test('A17: a script may signal its own children, and only its own', sandboxed, async () => {
  // `(target self)` is not enough: it denies the worker killing *its own
  // children*, so every cancellation path fails and a cancelled command keeps
  // running. `(target children)` is the fix, and it is only correct if it stays
  // scoped -- a bare `(allow signal)` would let a script signal any process on
  // the machine. Both directions are asserted, because relaxing the filter
  // "fixes" the first one while silently breaking the second.
  const outsider = spawn('sleep', ['120'], { stdio: 'ignore' });
  try {
    await withWorker({}, async ({ session, proj }) => {
      // A file, so shell quoting cannot be blamed for the result.
      await session.call('write', { path: 'signal.sh', content: [
        'sleep 30 &',
        'c=$!',
        'sleep 0.2',
        'kill $c',
        'echo "kill rc=$?"',
        'wait $c 2>/dev/null',
        'echo "wait rc=$?"',
        // A process this sandbox did not start: signalling it must be refused.
        `kill -0 ${outsider.pid} 2>&1 && echo "OUTSIDER_OK" || echo "OUTSIDER_REFUSED"`,
      ].join('\n') });

      const out = bashOut(await session.call('bash', { command: 'sh signal.sh' }));
      assert.equal(out.status, 'success', JSON.stringify(out));
      assert.match(out.output, /kill rc=0/, 'signalling its own child must be permitted');
      assert.doesNotMatch(out.output, /kill rc=[1-9]/, 'a cancelled child must really be gone');
      assert.match(out.output, /OUTSIDER_REFUSED/,
        'a process the sandbox did not start must not be signalable');
      assert.doesNotMatch(out.output, /OUTSIDER_OK/);
      assert.equal(isAlive(outsider.pid), true, 'the outsider must be untouched');
      assert.equal(fs.existsSync(path.join(proj, 'signal.sh')), true);
    });
  } finally {
    try { outsider.kill('SIGKILL'); } catch { /* already gone */ }
  }
});

test('A17: a tool past its budget is stopped, not left running', sandboxed, async () => {
  let commandPid = 0;
  let backgroundPid = 0;
  await withWorker({ callTimeoutMs: 600 }, async ({ session, proj }) => {
    // Two things to leak: the command itself, and the child it backgrounds.
    // Asserting only that the *worker* stopped would pass even if the command
    // kept running -- which is the failure A17 is actually about.
    const reply = await session.call('bash', {
      command: 'echo $$ > shell.pid; sleep 120 & echo $! > bg.pid; sleep 120',
    }, 600);
    const outcome = sessionMod.classifyReply(reply);
    assert.equal(outcome.kind, 'protocol_error', JSON.stringify(reply));
    assert.equal(outcome.code, 'timeout');

    // A timeout reply carries no output, so the pids are read from the project.
    // `$$` and `$!` are written by the shell itself, so their presence is proof
    // the command really ran -- and identifying what leaked is the whole
    // assertion, since the frame that would have carried it was never sent.
    await new Promise((resolve) => setTimeout(resolve, 500));
    commandPid = Number(fs.readFileSync(path.join(proj, 'shell.pid'), 'utf8').trim());
    backgroundPid = Number(fs.readFileSync(path.join(proj, 'bg.pid'), 'utf8').trim());
    assert.ok(commandPid > 1 && backgroundPid > 1, `${commandPid} / ${backgroundPid}`);

    await new Promise((resolve) => setTimeout(resolve, 2500));
    assert.equal(session.running, false, 'the worker tree must be gone');
  });
  assert.equal(await waitForGone(backgroundPid), true,
    `the command's background child ${backgroundPid} outlived the timeout`);
  assert.equal(await waitForGone(commandPid), true,
    `the timed-out command ${commandPid} is still running`);
});

test('a confined worker reads and writes the project', { skip: !(onMac && seatbelt) }, async () => {
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-bound-')));
  const proj = path.join(tmp, 'proj');
  const run = path.join(tmp, 'run');
  fs.mkdirSync(proj);
  fs.mkdirSync(run);
  fs.writeFileSync(path.join(proj, 'inside.txt'), 'hello\n');

  const plan = planFor(root, proj, run);
  assert.equal(plan.ok, true, JSON.stringify(plan));

  const session = new sessionMod.WorkerSession(plan, {}, { callTimeoutMs: 60000 });
  try {
    const hello = await session.hello(proj, run);
    assert.equal(hello.status, 'success', JSON.stringify(hello));
    assert.equal(hello.root, fs.realpathSync(proj));

    const read = await session.call('read', { path: 'inside.txt' });
    assert.equal(read.status, 'success', JSON.stringify(read));
    assert.match(JSON.stringify(read.result), /hello/);

    const write = await session.call('write', { path: 'out.txt', content: 'made' });
    assert.equal(write.status, 'success', JSON.stringify(write));
    assert.equal(fs.readFileSync(path.join(proj, 'out.txt'), 'utf8'), 'made');
  } finally {
    await session.stop();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test('a tool that failed is not reported as a broken worker', () => {
  // Observed on the wire: bash refused to open /dev/null and the worker answered
  // with the tool's own failure, carrying `result` and no `error`.
  const toolFailure = {
    id: 'a', status: 'error', tool: 'bash',
    result: "Error executing command: [Errno 1] Operation not permitted: '/dev/null'",
    display: null, duration_ms: 0,
  };
  const classified = sessionMod.classifyReply(toolFailure);
  assert.equal(classified.kind, 'tool_error');
  assert.equal(classified.tool, 'bash');
  assert.match(String(classified.result), /Operation not permitted/);

  // A protocol failure is the other shape: an `error` object and no tool.
  const protocolFailure = {
    id: 'a', status: 'error',
    error: { code: 'unknown_tool', message: 'no such tool' },
  };
  assert.deepEqual(sessionMod.classifyReply(protocolFailure), {
    kind: 'protocol_error', code: 'unknown_tool', message: 'no such tool',
  });

  const success = { id: 'a', status: 'success', tool: 'read', result: { content: 'hi' } };
  assert.equal(sessionMod.classifyReply(success).kind, 'tool_result');
});

test('the sandbox, not a string check, blocks a write outside the project',
  { skip: !(onMac && seatbelt) }, async () => {
    const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-escape-')));
    const proj = path.join(tmp, 'proj');
    const outside = path.join(tmp, 'outside');
    const run = path.join(tmp, 'run');
    fs.mkdirSync(proj);
    fs.mkdirSync(outside);
    fs.mkdirSync(run);
    fs.writeFileSync(path.join(outside, 'secret.txt'), 'do not read\n');

    const plan = planFor(root, proj, run);
    const session = new sessionMod.WorkerSession(plan, {}, { callTimeoutMs: 60000 });
    try {
      const hello = await session.hello(proj, run);
      assert.equal(hello.status, 'success', JSON.stringify(hello));

      // A shell command is deliberately *not* filtered by the worker, so this
      // reaches the OS: the denial here is the kernel's, not a substring match.
      const writeOut = await session.call('bash',
        { command: `echo pwned > ${JSON.stringify(path.join(outside, 'escape.txt'))}` });
      assert.equal(
        fs.existsSync(path.join(outside, 'escape.txt')), false,
        'the sandbox must block a write outside the project');
      assert.equal(sessionMod.classifyReply(writeOut).kind, 'tool_error',
        'the tool must report the kernel denial, not silently succeed');

      // Reads outside the project are refused *by the kernel*. Asserted through
      // `bash` on purpose: the `read` tool has its own path guard, which would
      // make this pass even if the sandbox said nothing.
      const readOut = await session.call('bash',
        { command: `cat ${JSON.stringify(path.join(outside, 'secret.txt'))}` });
      assert.equal(sessionMod.classifyReply(readOut).kind, 'tool_error',
        'the kernel must refuse a read outside the project');
      const text = JSON.stringify(readOut.result || '') + JSON.stringify(readOut.display || '');
      assert.equal(text.includes('do not read'), false, 'the contents must not leak');
      assert.match(text, /Operation not permitted/);

      // And the project itself is still usable from the same shell.
      const insideOk = await session.call('bash', { command: 'echo fine > ok.txt' });
      assert.equal(insideOk.status, 'success', JSON.stringify(insideOk));
      assert.equal(fs.readFileSync(path.join(proj, 'ok.txt'), 'utf8').trim(), 'fine');
    } finally {
      await session.stop();
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  });

test('a directory name cannot widen the sandbox, however hostile it is',
  { skip: !(onMac && seatbelt) }, async () => {
    const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-inject-')));
    // A name that, if it were interpolated unescaped, would close the literal and
    // grant write access to the whole filesystem. Quotes and newlines are both
    // legal in a macOS filename, so this is reachable, not hypothetical.
    const proj = path.join(tmp, 'co"w)\n(allow file-write* (subpath "/")');
    const run = path.join(tmp, 'run');
    const outside = path.join(tmp, 'outside.txt');
    fs.mkdirSync(proj, { recursive: true });
    fs.mkdirSync(run);

    const plan = planFor(root, proj, run);
    assert.equal(plan.ok, true, JSON.stringify(plan));

    const session = new sessionMod.WorkerSession(plan, {}, { callTimeoutMs: 60000 });
    try {
      const hello = await session.hello(proj, run);
      assert.equal(hello.status, 'success', JSON.stringify(hello));

      const out = await session.call('bash', {
        command: `echo pwned > ${JSON.stringify(outside)}`,
      });
      assert.equal(fs.existsSync(outside), false,
        'a hostile directory name must not grant write access outside it');
      assert.notEqual(out.status, 'success', JSON.stringify(out));

      // The project itself is still usable, so the profile was not simply broken.
      const inside = await session.call('write', { path: 'ok.txt', content: 'made' });
      assert.equal(inside.status, 'success', JSON.stringify(inside));
      assert.equal(fs.readFileSync(path.join(proj, 'ok.txt'), 'utf8'), 'made');
    } finally {
      await session.stop();
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  });

// ---------------------------------------------------------------------------
// The installed shape (task 4.9's "随包 Python" leg, task 10.2's local leg)
// ---------------------------------------------------------------------------
//
// Everything above runs against a source checkout by default. That is *not* the
// shape a user has, and the two are not interchangeable:
//
//   * a checkout runs `python -m agent.desktop_local.worker` from a virtualenv
//     whose `base_prefix` and `site-packages` are real, readable directories;
//   * an installed app runs one frozen executable, with no virtualenv at all,
//     where `base_prefix`/`stdlib`/`purelib` still name the **build machine** and
//     `interpreter.ts` therefore grants only `bundleRoot`.
//
// So a profile that works in a checkout can fail once frozen -- the failure the
// launcher's own header calls "the worker crashed" -- and a profile that is
// *wider* in a checkout proves nothing about a user's. Set `COW_A26_BACKEND` to
// a `resources/backend` directory (e.g. inside a built `.app`) to re-assert the
// claims on the installed shape:
//
//   COW_A26_BACKEND=/Applications/Cow.app/Contents/Resources/backend \
//     node --test tests/test_desktop_local_execution.cjs
//
// The escape probes then run with the *system* `/usr/bin/python3` rather than
// the app's interpreter (`probePythonFor`), which makes the kernel claim
// stronger: the confined process is one the app never shipped.

const installed = {
  skip: isInstalled() ? false : 'set COW_A26_BACKEND to a packaged resources/backend to run this',
};

test('installed: the runtime comes from the bundle, and the build machine is not named',
  installed, () => {
    const { backendPath, runtime } = runtimeFor(root);
    assert.equal(runtime.frozen, true);
    assert.equal(runtime.path, path.join(backendPath, 'cowagent-backend', 'cowagent-backend'));

    const probe = new interpreterMod.InterpreterRegistry().get(runtime);
    assert.ok(probe, 'the bundle must answer its own probe');
    assert.equal(probe.frozen, true);

    const readPaths = interpreterMod.interpreterReadPaths(probe);
    // The whole point of the frozen branch: only the bundle. A build machine's
    // prefix would widen the sandbox on the machine that built it and grant
    // nothing on a user's -- so it must not appear even when it exists here.
    for (const granted of readPaths) {
      assert.ok(
        granted.startsWith(probe.bundleRoot) || granted.startsWith(path.dirname(probe.realPath)),
        `an installed run must not be granted ${granted}`,
      );
      assert.equal(granted.includes('.venv'), false, `no virtualenv in ${granted}`);
    }
    // The specific leak the frozen branch exists to prevent: `sysconfig` on a
    // frozen build still answers with the **build machine's** stdlib and
    // site-packages. Both are real directories here (the drill ran on the machine
    // that built the bundle), so a `source`-shaped read list would have granted
    // them and this is not a vacuous check.
    assert.equal(readPaths.includes(probe.stdlib), false,
      `the build machine's stdlib must not be granted: ${probe.stdlib}`);
    assert.equal(readPaths.includes(probe.purelib), false,
      `the build machine's site-packages must not be granted: ${probe.purelib}`);
    assert.ok(probe.stdlib && probe.purelib, 'the probe must have reported them for this to mean anything');
    // `base_prefix` on a frozen build *is* the bundle, so it does not leak the
    // build machine the way a virtualenv's does; asserted rather than assumed.
    assert.equal(probe.basePrefix, probe.bundleRoot);
  });

test('installed: the frozen worker starts inside the sandbox and offers the master tools',
  installed, async () => {
    await withWorker({}, async ({ session, hello }) => {
      // A missing read allowance kills the process *before* the handshake, which
      // the app can only report as a crash. This is the assertion that catches it.
      assert.equal(hello.status, 'success');
      assert.deepEqual(
        [...hello.tools].sort(),
        ['bash', 'edit', 'ls', 'read', 'search_files', 'write'],
        'an installed app runs the same tool set as a checkout',
      );
      assert.ok(Number.isInteger(hello.pid) && hello.pid > 0);
    });
  });

test('installed: the frozen worker writes and reads its project', installed, async () => {
  await withWorker({}, async ({ session, proj }) => {
    const made = await session.call('write', { path: 'made.txt', content: 'frozen\n' });
    assert.equal(made.status, 'success', JSON.stringify(made));
    assert.equal(fs.readFileSync(path.join(proj, 'made.txt'), 'utf8'), 'frozen\n');
    const read = await session.call('read', { path: 'made.txt' });
    assert.equal(read.status, 'success', JSON.stringify(read));
    assert.match(read.result.content, /frozen/);
    // A shell, run by the frozen interpreter's own bash tool.
    const shell = bashOut(await session.call('bash', { command: 'cat made.txt' }));
    assert.equal(shell.output.trim(), 'frozen');
  });
});

test('installed: the kernel, not the worker path guard, stops an escape', installed, async () => {
  await withWorker({}, async ({ session, tmp }) => {
    const secret = path.join(tmp, 'secret.txt');
    fs.writeFileSync(secret, 'DO-NOT-LEAK\n');

    // The worker's own guards refuse these first (`path_outside_project`), which
    // is why the kernel claim has to be made through `bash`: a shell command is
    // not path-checked by the worker, so anything the kernel allows, happens.
    const bash = bashOut(await session.call('bash', { command: `cat ${JSON.stringify(secret)}` }));
    assert.equal(bash.exitCode, 1, JSON.stringify(bash));
    assert.match(bash.output, /Operation not permitted/,
      'the seatbelt must refuse it, not merely report an error');
    assert.equal(bash.output.includes('DO-NOT-LEAK'), false, 'the contents must not leak');

    const write = bashOut(await session.call('bash', {
      command: `echo pwned > ${JSON.stringify(path.join(tmp, 'pwn.txt'))}`,
    }));
    assert.equal(fs.existsSync(path.join(tmp, 'pwn.txt')), false,
      'a write outside the project must not land');
    assert.notEqual(write.exitCode, 0, JSON.stringify(write));

    // The worker's tools refuse by their own guard *and* the kernel backs it up:
    // both are asserted, because either alone would be a weaker statement.
    const toolRead = await session.call('read', { path: secret });
    assert.notEqual(toolRead.status, 'success', JSON.stringify(toolRead));
    const toolWrite = await session.call('write', { path: path.join(tmp, 'pwn2.txt'), content: 'x' });
    assert.notEqual(toolWrite.status, 'success', JSON.stringify(toolWrite));
    assert.equal(fs.existsSync(path.join(tmp, 'pwn2.txt')), false);

    // A shell still cannot reach the network, and the project stays usable -- so
    // the boundary is a boundary, not a broken profile.
    const net = bashOut(await session.call('bash', {
      command: 'nc -z 127.0.0.1 22; echo none',
    }));
    assert.equal(net.output.includes('succeeded'), false, JSON.stringify(net));
    const inside = bashOut(await session.call('bash', { command: 'echo fine > ok.txt' }));
    assert.equal(inside.exitCode, 0, JSON.stringify(inside));
  });
});

test('installed: no launcher secret reaches the bundle side', installed, async () => {
  await withWorker({
    env: {
      ...process.env,
      COW_DESKTOP_TOKEN: 'launch-secret',
      COW_IDENTITY_DB: path.join(root, 'identity.db'),
      COW_SESSION_TOKEN: 'bearer-should-not-travel',
    },
  }, async ({ session, proj }) => {
    // Read through the worker's own shell, so the environment being inspected is
    // the one the installed worker really has (a Python probe would need an
    // interpreter the installed profile does not grant -- see `probePythonFor`).
    const env = bashOut(await session.call('bash', { command: 'env' }));
    assert.equal(env.exitCode, 0, JSON.stringify(env));
    for (const secret of ['launch-secret', 'bearer-should-not-travel']) {
      assert.equal(env.output.includes(secret), false, `${secret} must not reach the worker`);
    }
    // The identity database is not merely unmentioned but unreachable.
    const reach = bashOut(await session.call('bash', {
      command: `cat ${JSON.stringify(path.join(root, 'identity.db'))} 2>&1 | head -c 200`,
    }));
    assert.match(reach.output, /Operation not permitted|No such file/,
      `the identity database must be out of reach: ${JSON.stringify(reach)}`);
  });
});
