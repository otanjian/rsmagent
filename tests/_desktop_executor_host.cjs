// The *real* desktop executor endpoint, stood up for the Python side of the
// cross-language acceptance run (change task 5.4).
//
// Run: node tests/_desktop_executor_host.cjs <projectRoot> [--user u1] [--device d1]
//
// It prints one JSON line on stdout -- {"origin": "http://127.0.0.1:<port>",
// "token": "..."} -- and then stays up until its stdin closes or it is
// signalled. The Python acceptance test puts those two values in
// COW_DESKTOP_EXECUTOR_URL / COW_DESKTOP_EXECUTOR_TOKEN, so the backend under
// test reaches the platform launcher the way the shell wires it in production:
// a real loopback listener, a real launch token, a real sandboxed worker, and a
// real command whose file either appears in the project or does not.
//
// Nothing is stubbed: the grant table below is the same shape the main process
// keeps in memory, and the endpoint module is the compiled production one.

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const dist = path.join(desktop, 'dist', 'main', 'local-execution');

function flag(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  if (index === -1 || index === process.argv.length - 1) return fallback;
  return process.argv[index + 1];
}

// Repeatable: node tests/_desktop_executor_host.cjs <proj> --skill-cache-root <dir>
function flags(name) {
  const values = [];
  process.argv.forEach((value, index) => {
    if (value !== `--${name}`) return;
    if (index === process.argv.length - 1) return;
    values.push(process.argv[index + 1]);
  });
  return values;
}

const projectRoot = fs.realpathSync(process.argv[2]);
const userId = flag('user', 'u1');
const tenantId = flag('tenant', 't1');
const deviceId = flag('device', 'd1');
const grantVersion = Number(flag('version', '1'));
const purpose = flag('purpose', 'project-execution');

function compileIfStale() {
  const sources = [
    'launch-token.ts', 'root-registration.ts', 'executor-endpoint.ts',
    'sandbox.ts', 'env.ts', 'worker-session.ts', 'interpreter.ts', 'launch.ts',
  ].map((name) => path.join(desktop, 'src', 'main', 'local-execution', name));
  const stale = sources.some((src) => {
    const compiled = path.join(dist, path.basename(src).replace(/\.ts$/, '.js'));
    return !fs.existsSync(compiled) || fs.statSync(compiled).mtimeMs < fs.statSync(src).mtimeMs;
  });
  if (stale) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
}

compileIfStale();

const endpointsMod = require(path.join(dist, 'executor-endpoint.js'));
const interpreterMod = require(path.join(dist, 'interpreter.js'));

const venvPython = path.join(root, '.venv', 'bin', 'python');
const pythonPath = fs.existsSync(venvPython) ? venvPython : (process.env.PYTHON || 'python3');
// The live grant table, in the shape the main process keeps: identifiers only,
// with the directory held beside them rather than in the record a request sees.
const grants = [{
  id: 'grant-e2e',
  userId,
  tenantId,
  deviceId,
  grantVersion,
  purpose,
}];

(async () => {
  const registry = new interpreterMod.InterpreterRegistry();
  const executor = await endpointsMod.startLocalExecutor({
    grants: {
      list: () => grants,
      absolutePathFor: (id) => (id === 'grant-e2e' ? projectRoot : null),
    },
    backendPath: root,
    // The harness runs the worker the way a source checkout does. The packaged
    // shape (the bundle's own binary) is what `interpreter.ts` resolves from the
    // bundle layout, and is covered by its own tests.
    runtime: interpreterMod.sourceRuntime(pythonPath),
    probe: (candidate) => registry.get(candidate),
    platform: process.platform,
    seatbeltAvailable: fs.existsSync('/usr/bin/sandbox-exec'),
    baseEnv: process.env,
    callTimeoutMs: 120000,
    // The harness plays the role the shell plays in production: it is the only
    // party that knows where the skill cache lives, and the only party allowed to
    // decide which directory a run may read (task 8.8).
    skillCacheRoot: flags('skill-cache-root')[0],
  });

  const stop = async () => {
    try {
      await executor.close();
    } catch {
      /* the worker may already be gone */
    }
    process.exit(0);
  };

  process.stdout.write(`${JSON.stringify({ origin: executor.origin, token: executor.token })}\n`);
  process.stdin.on('end', stop);
  process.stdin.resume();
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
})().catch((error) => {
  process.stderr.write(`executor host failed: ${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
