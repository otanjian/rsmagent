// The local executor endpoint: the backend's only way to run a script.
//
// Run: node --test tests/test_desktop_local_executor_endpoint.cjs
//
// What is verified here is the *surface* the same-machine backend is given, and
// the two halves are deliberately different:
//
//   1. everything a caller must not be able to do -- reach it without the launch
//      token, from a document origin, with a tool it does not offer, with a
//      directory outside the granted root, or with a stale/read-only grant -- is
//      refused *before* any process is started;
//   2. the one thing it must do -- run the project's `bash` tool inside the real
//      platform sandbox and hand back the real result -- is asserted by running
//      it: a real sandboxed worker, a real command, a file that either appears in
//      the project on disk or does not.
//
// The sandbox itself is group 4's (see tests/test_desktop_local_execution.cjs);
// nothing here re-asserts kernel behaviour, and nothing stands in for it.

const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync, spawn } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const dist = path.join(desktop, 'dist', 'main', 'local-execution');

let tokenMod;
let endpointsMod;
let registrationMod;
let interpreterMod;

before(() => {
  const sources = [
    'launch-token.ts',
    'root-registration.ts',
    'executor-endpoint.ts',
    'sandbox.ts',
    'env.ts',
    'worker-session.ts',
    'interpreter.ts',
    'launch.ts',
  ].map((name) => path.join(desktop, 'src', 'main', 'local-execution', name));
  const stale = sources.some((src) => {
    const compiled = path.join(dist, path.basename(src).replace(/\.ts$/, '.js'));
    return !fs.existsSync(compiled) || fs.statSync(compiled).mtimeMs < fs.statSync(src).mtimeMs;
  });
  if (stale) {
    execFileSync('npx', ['tsc', '-p', 'tsconfig.main.json'], { cwd: desktop, stdio: 'pipe' });
  }
  tokenMod = require(path.join(dist, 'launch-token.js'));
  endpointsMod = require(path.join(dist, 'executor-endpoint.js'));
  registrationMod = require(path.join(dist, 'root-registration.js'));
  interpreterMod = require(path.join(dist, 'interpreter.js'));
});

// ---------------------------------------------------------------------------
// The launch token
// ---------------------------------------------------------------------------

test('the launch token is minted once per run and reused', () => {
  tokenMod.setDesktopLaunchToken(null);
  assert.equal(tokenMod.desktopLaunchToken(), null);
  const first = tokenMod.ensureDesktopLaunchToken();
  assert.equal(first.length >= 40, true);
  assert.equal(tokenMod.ensureDesktopLaunchToken(), first, 'a second call must reuse it');
  assert.deepEqual(tokenMod.desktopTokenHeaders(first), { 'x-cow-desktop-token': first });
  assert.deepEqual(tokenMod.desktopTokenHeaders(null), {});
  tokenMod.setDesktopLaunchToken('   ');
  assert.equal(tokenMod.desktopLaunchToken(), null, 'blank is not a token');
  tokenMod.setDesktopLaunchToken(null);
});

test('two mints are different', () => {
  const a = tokenMod.newLaunchToken();
  const b = tokenMod.newLaunchToken();
  assert.notEqual(a, b);
});

// ---------------------------------------------------------------------------
// Registering the picked root with the same-machine backend
// ---------------------------------------------------------------------------

function withBackend(handler, body) {
  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      const chunks = [];
      req.on('data', (chunk) => chunks.push(chunk));
      req.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf8');
        const result = handler({
          method: req.method,
          url: req.url,
          headers: req.headers,
          body: raw ? JSON.parse(raw) : null,
        });
        const data = JSON.stringify(result.body ?? {});
        res.writeHead(result.status ?? 200, { 'Content-Type': 'application/json' });
        res.end(data);
      });
    });
    server.listen(0, '127.0.0.1', async () => {
      try {
        resolve(await body(`http://127.0.0.1:${server.address().port}`));
      } catch (err) {
        reject(err);
      } finally {
        server.close();
      }
    });
  });
}

test('the root is registered with the launch token and a native bearer only', async () => {
  let seen = null;
  await withBackend((request) => {
    seen = request;
    return { status: 200, body: { project_mode: 'project-execution', grant_version: 4 } };
  }, async (origin) => {
    const result = await registrationMod.registerLocalRoot({
      origin,
      launchToken: 'tok-1',
      nativeBearer: 'native-abc',
      deviceId: 'd1',
      workspaceId: 'w1',
      bindingId: 'b1',
      absolutePath: '/Users/someone/项目 with spaces',
      projectMode: 'project-execution',
    });
    assert.deepEqual(result, { ok: true, projectMode: 'project-execution', grantVersion: 4 });
  });
  assert.equal(seen.url, '/api/desktop/local-roots');
  assert.equal(seen.method, 'POST');
  assert.equal(seen.headers['x-cow-desktop-token'], 'tok-1');
  assert.equal(seen.headers.authorization, 'Bearer native-abc');
  assert.equal(seen.body.absolute_path, '/Users/someone/项目 with spaces');
  assert.equal(seen.body.project_mode, 'project-execution');
});

test('a remote origin is refused before anything is sent', async () => {
  const result = await registrationMod.registerLocalRoot({
    origin: 'https://server.example.com',
    launchToken: 'tok-1',
    nativeBearer: 'native-abc',
    deviceId: 'd1',
    workspaceId: 'w1',
    bindingId: 'b1',
    absolutePath: '/tmp/proj',
    projectMode: 'project-execution',
    fetchFn: () => {
      throw new Error('must not be called');
    },
  });
  assert.equal(result.ok, false);
  assert.equal(result.code, 'invalid_request');
});

test('missing credentials mean nothing is sent', async () => {
  const base = {
    origin: 'http://127.0.0.1:1',
    deviceId: 'd1',
    workspaceId: 'w1',
    bindingId: 'b1',
    absolutePath: '/tmp/proj',
    projectMode: 'project-execution',
    fetchFn: () => {
      throw new Error('must not be called');
    },
  };
  for (const [patch, code] of [
    [{ launchToken: null, nativeBearer: 'n' }, 'desktop_token_required'],
    [{ launchToken: 't', nativeBearer: null }, 'permission_denied'],
    [{ launchToken: 't', nativeBearer: 'n', deviceId: '' }, 'invalid_request'],
  ]) {
    const result = await registrationMod.registerLocalRoot({ ...base, ...patch });
    assert.equal(result.ok, false, JSON.stringify(result));
    assert.equal(result.code, code);
  }
});

test('the backend refusal is reported as-is', async () => {
  await withBackend(() => ({
    status: 403,
    body: { code: 'permission_denied', message: 'this workspace was not authorized for local execution' },
  }), async (origin) => {
    const result = await registrationMod.registerLocalRoot({
      origin,
      launchToken: 'tok-1',
      nativeBearer: 'native-abc',
      deviceId: 'd1',
      workspaceId: 'w1',
      bindingId: 'b1',
      absolutePath: '/tmp/proj',
      projectMode: 'project-execution',
    });
    assert.equal(result.ok, false);
    assert.equal(result.code, 'permission_denied');
    assert.match(result.message, /not authorized/);
  });
});

test('binding a chat names the identifiers, never a path', async () => {
  let seen = null;
  await withBackend((request) => {
    seen = request;
    return { status: 200, body: {} };
  }, async (origin) => {
    const result = await registrationMod.bindSessionTarget({
      origin,
      launchToken: 'tok-1',
      nativeBearer: 'native-abc',
      agentId: 'agent-a',
      sessionId: 's/1',   // encoded in the path, not interpolated raw
      deviceId: 'd1',
      workspaceId: 'w1',
      bindingId: 'b1',
      projectMode: 'project-execution',
    });
    assert.deepEqual(result, { ok: true });
  });
  assert.equal(seen.url, '/api/desktop/sessions/s%2F1/execution-target');
  assert.equal(seen.body.binding_id, 'b1');
  assert.equal('absolute_path' in seen.body, false);
});

test('closing the project asks the backend to forget the root', async () => {
  let seen = null;
  await withBackend((request) => {
    seen = request;
    return { status: 200, body: { revoked: 1 } };
  }, async (origin) => {
    const result = await registrationMod.clearLocalRoot({
      origin, launchToken: 'tok-1', nativeBearer: 'native-abc', deviceId: 'd1',
    });
    assert.equal(result.ok, true);
  });
  assert.equal(seen.method, 'DELETE');
  assert.equal(seen.body.device_id, 'd1');
});

// ---------------------------------------------------------------------------
// The endpoint: refusals, before any process exists
// ---------------------------------------------------------------------------

/** A grant source that never resolves a grant: used by the refusal tests. */
function emptyGrants() {
  return { list: () => [], absolutePathFor: () => null };
}

function liveGrant(projectRoot, { purpose = 'project-execution', version = 1, id = 'grant-1' } = {}) {
  return {
    list: () => [{
      id, userId: 'u1', tenantId: 't1', deviceId: 'd1',
      grantVersion: version, purpose,
    }],
    absolutePathFor: (requested) => (requested === id ? projectRoot : null),
  };
}

async function withExecutor(config, body) {
  const executor = await endpointsMod.startLocalExecutor({
    backendPath: root,
    runtime: null,
    probe: () => null,
    platform: 'win32',          // no probed boundary: the launcher refuses
    token: 'exec-token',
    tempBase: fs.mkdtempSync(path.join(os.tmpdir(), 'cow-exec-')),
    ...config,
  });
  try {
    await body(executor);
  } finally {
    await executor.close();
  }
}

function post(executor, pathname, body, headers = {}) {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify(body), 'utf8');
    const request = http.request({
      host: '127.0.0.1',
      port: Number(new URL(executor.origin).port),
      path: pathname,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': String(data.length),
        Authorization: `Bearer ${executor.token}`,
        ...headers,
      },
    }, (res) => {
      let text = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { text += chunk; });
      res.on('end', () => resolve({
        status: res.statusCode,
        body: text ? JSON.parse(text) : {},
      }));
    });
    request.on('error', reject);
    request.write(data);
    request.end();
  });
}

test('the endpoint listens on loopback only and needs the launch token', async () => {
  await withExecutor({ grants: liveGrant('/tmp') }, async (executor) => {
    assert.match(executor.origin, /^http:\/\/127\.0\.0\.1:\d+$/);
    const noToken = await post(executor, '/desktop-exec/script', { tool: 'bash' }, { Authorization: '' });
    assert.equal(noToken.status, 403);
    assert.equal(noToken.body.code, 'desktop_token_required');
    const wrongToken = await post(executor, '/desktop-exec/script', { tool: 'bash' }, { Authorization: 'Bearer nope' });
    assert.equal(wrongToken.status, 403);
    // A document origin is refused even with the right token: a page must not be
    // able to reach this listener by learning the port.
    const fromDocument = await post(executor, '/desktop-exec/script', { tool: 'bash' }, { Origin: 'http://localhost:9899' });
    assert.equal(fromDocument.status, 403);
    assert.equal(fromDocument.body.code, 'permission_denied');
  });
});

test('only the two operations exist, and only POST reaches them', async () => {
  await withExecutor({ grants: emptyGrants() }, async (executor) => {
    const unknown = await post(executor, '/desktop-exec/anything', {});
    assert.equal(unknown.status, 404);
  });
});

test('the endpoint runs bash and nothing else', async () => {
  await withExecutor({ grants: emptyGrants() }, async (executor) => {
    const result = await post(executor, '/desktop-exec/script', {
      tool: 'python', arguments: { command: 'print(1)' }, cwd: '/tmp',
      scope: { user_id: 'u1', device_id: 'd1', grant_version: 1 },
    });
    assert.equal(result.status, 400);
    assert.equal(result.body.code, 'unknown_tool');
  });
});

test('a grant that does not match the run is refused, not guessed', async () => {
  const projectRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'cow-grant-'));
  const scope = { user_id: 'u1', tenant_id: 't1', device_id: 'd1', grant_version: 1 };
  const cases = [
    ['a stale version', liveGrant(projectRoot, { version: 2 }), { ...scope, grant_version: 1 }],
    ['a read-only grant', liveGrant(projectRoot, { purpose: 'readonly-input' }), scope],
    ['another user', liveGrant(projectRoot), { ...scope, user_id: 'u2' }],
    ['no grant at all', emptyGrants(), scope],
  ];
  for (const [what, grants, requested] of cases) {
    await withExecutor({ grants }, async (executor) => {
      const result = await post(executor, '/desktop-exec/script', {
        tool: 'bash', arguments: { command: 'echo hi' }, cwd: projectRoot, scope: requested,
      });
      assert.equal(result.status, 403, `${what}: ${JSON.stringify(result)}`);
      assert.equal(result.body.code, 'grant_revoked', what);
    });
  }
  fs.rmSync(projectRoot, { recursive: true, force: true });
});

test('two live grants in one scope are refused rather than picked', async () => {
  const projectRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'cow-grant-'));
  const grants = {
    list: () => [
      { id: 'a', userId: 'u1', tenantId: 't1', deviceId: 'd1', grantVersion: 1, purpose: 'project-execution' },
      { id: 'b', userId: 'u1', tenantId: 't1', deviceId: 'd1', grantVersion: 1, purpose: 'project-execution' },
    ],
    absolutePathFor: () => projectRoot,
  };
  await withExecutor({ grants }, async (executor) => {
    const result = await post(executor, '/desktop-exec/script', {
      tool: 'bash', arguments: { command: 'echo hi' }, cwd: projectRoot,
      scope: { user_id: 'u1', tenant_id: 't1', device_id: 'd1', grant_version: 1 },
    });
    assert.equal(result.status, 403);
    assert.equal(result.body.code, 'ambiguous_grant');
  });
  fs.rmSync(projectRoot, { recursive: true, force: true });
});

test('a directory outside the granted root is refused', async () => {
  const projectRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'cow-grant-'));
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'cow-outside-'));
  await withExecutor({ grants: liveGrant(projectRoot) }, async (executor) => {
    const result = await post(executor, '/desktop-exec/script', {
      tool: 'bash', arguments: { command: 'echo hi' }, cwd: outside,
      scope: { user_id: 'u1', tenant_id: 't1', device_id: 'd1', grant_version: 1 },
    });
    assert.equal(result.status, 400);
    assert.equal(result.body.code, 'path_outside_project');
  });
  fs.rmSync(projectRoot, { recursive: true, force: true });
  fs.rmSync(outside, { recursive: true, force: true });
});

test('a platform with no probed boundary answers with its own reason', async () => {
  await withExecutor({ grants: liveGrant('/tmp') }, async (executor) => {
    const result = await post(executor, '/desktop-exec/capabilities', {});
    assert.equal(result.status, 200);
    assert.equal(result.body.supported, false);
    assert.equal(result.body.code, 'unsupported_platform');
    // The wording must not read as "a permission you could grant".
    assert.match(result.body.reason, /not a permissions problem/);
  });
});

test('a revoked grant is reported as a revoked grant, not a missing launcher', async () => {
  await withExecutor({ grants: emptyGrants() }, async (executor) => {
    const result = await post(executor, '/desktop-exec/capabilities', {
      scope: { user_id: 'u1', tenant_id: 't1', device_id: 'd1', grant_version: 1 },
    });
    assert.equal(result.status, 200);
    assert.equal(result.body.supported, false);
    assert.equal(result.body.code, 'grant_revoked');
  });
});

// ---------------------------------------------------------------------------
// The endpoint, for real: a sandboxed worker runs the master bash tool
// ---------------------------------------------------------------------------

const onMac = process.platform === 'darwin';
const seatbelt = fs.existsSync('/usr/bin/sandbox-exec');
const sandboxed = { skip: !(onMac && seatbelt) };

function pythonFor(repoRoot) {
  const venv = path.join(repoRoot, '.venv', 'bin', 'python');
  return fs.existsSync(venv) ? venv : process.env.PYTHON || 'python3';
}

async function withRealExecutor(projectRoot, body) {
  const spawns = [];
  const spawnFn = (...args) => {
    spawns.push(args);
    return spawn(...args);
  };
  const registry = new interpreterMod.InterpreterRegistry();
  const executor = await endpointsMod.startLocalExecutor({
    grants: liveGrant(projectRoot),
    backendPath: root,
    runtime: interpreterMod.sourceRuntime(pythonFor(root)),
    probe: (candidate) => registry.get(candidate),
    platform: process.platform,
    seatbeltAvailable: true,
    baseEnv: process.env,
    token: 'exec-token',
    spawnFn,
    callTimeoutMs: 60000,
  });
  try {
    await body({ executor, spawns, projectRoot });
  } finally {
    await executor.close();
  }
}

test('a real command runs in the project and its output comes back', sandboxed, async () => {
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-exec-real-')));
  const project = path.join(tmp, 'proj');
  fs.mkdirSync(project, { recursive: true });
  const scope = { user_id: 'u1', tenant_id: 't1', device_id: 'd1', grant_version: 1 };
  await withRealExecutor(project, async ({ executor, spawns }) => {
    const result = await post(executor, '/desktop-exec/script', {
      tool: 'bash',
      arguments: { command: 'pwd && echo 生成的文件 > "结果 note.txt" && ls' },
      cwd: project,
      scope,
    });
    assert.equal(result.status, 200, JSON.stringify(result.body));
    assert.equal(result.body.status, 'success');
    const output = String(result.body.result?.output ?? '');
    // The command ran *in the project*: the master tool's own result shape.
    assert.match(output, new RegExp(project.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
    assert.match(output, /结果 note\.txt/);
    assert.equal(result.body.result?.exit_code, 0);
    // ...and the file is really there, written by a real process.
    assert.equal(fs.readFileSync(path.join(project, '结果 note.txt'), 'utf8').trim(), '生成的文件');
    // One worker for the grant: a second call reuses it rather than spawning again.
    const second = await post(executor, '/desktop-exec/script', {
      tool: 'bash', arguments: { command: 'echo again' }, cwd: project, scope,
    });
    assert.equal(second.body.status, 'success');
    assert.equal(spawns.length, 1, 'a second call must reuse the running worker');
  });
  fs.rmSync(tmp, { recursive: true, force: true });
});

test('a failing command fails the way the model expects', sandboxed, async () => {
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-exec-fail-')));
  const project = path.join(tmp, 'proj');
  fs.mkdirSync(project, { recursive: true });
  await withRealExecutor(project, async ({ executor }) => {
    const result = await post(executor, '/desktop-exec/script', {
      tool: 'bash',
      arguments: { command: 'echo out; echo err 1>&2; exit 3' },
      cwd: project,
      scope: { user_id: 'u1', tenant_id: 't1', device_id: 'd1', grant_version: 1 },
    });
    assert.equal(result.status, 200);
    // The tool ran and reported the *command's* failure (the master tool's own
    // semantics for a non-zero exit), which is what the model must be able to see.
    assert.equal(result.body.status, 'error');
    assert.equal(result.body.result?.exit_code, 3);
    assert.match(String(result.body.result?.output ?? ''), /out/);
  });
  fs.rmSync(tmp, { recursive: true, force: true });
});

test('the capability text is this platform\'s, with the real writable roots', sandboxed, async () => {
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cow-exec-cap-')));
  const project = path.join(tmp, 'proj');
  fs.mkdirSync(project, { recursive: true });
  await withRealExecutor(project, async ({ executor }) => {
    const result = await post(executor, '/desktop-exec/capabilities', {
      scope: { user_id: 'u1', tenant_id: 't1', device_id: 'd1', grant_version: 1 },
    });
    assert.equal(result.status, 200, JSON.stringify(result.body));
    assert.equal(result.body.supported, true);
    assert.equal(result.body.kind, 'seatbelt');
    assert.match(String(result.body.description), /Platform: macOS/);
    assert.match(String(result.body.description), /Isolation: kernel-level sandbox/);
    assert.match(String(result.body.description), /Writable: .*proj/);
  });
  fs.rmSync(tmp, { recursive: true, force: true });
});
