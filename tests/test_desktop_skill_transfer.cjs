// Run: node --test tests/test_desktop_skill_transfer.cjs
//
// Task 8.9, device half: obtaining the skill versions a run was authorized with.
// The check that matters is not "did bytes arrive" but "are they the authorized
// bytes" -- so most of these tests are about what the delivery *refuses*, and
// about the fact that a failure leaves the run unable to proceed rather than
// falling back to whatever the device already had.

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { execFileSync } = require('node:child_process')

const ROOT = path.join(__dirname, '..')
const DESKTOP = path.join(ROOT, 'desktop')
const SRC_DIR = path.join(DESKTOP, 'src', 'main', 'project-execution')
const DIST_DIR = path.join(DESKTOP, 'dist', 'main', 'project-execution')

let pkg
let cacheMod
let transfer

function needsBuild() {
  const compiled = path.join(DIST_DIR, 'skill-transfer.js')
  if (!fs.existsSync(compiled)) return true
  const newest = Math.max(
    ...fs.readdirSync(SRC_DIR).filter((n) => n.endsWith('.ts'))
      .map((n) => fs.statSync(path.join(SRC_DIR, n)).mtimeMs),
  )
  return newest > fs.statSync(compiled).mtimeMs
}

test.before(() => {
  if (needsBuild()) {
    execFileSync('npm', ['run', 'build:main'], { cwd: DESKTOP, stdio: 'inherit' })
  }
  pkg = require(path.join(DIST_DIR, 'skill-package.js'))
  cacheMod = require(path.join(DIST_DIR, 'skill-cache.js'))
  transfer = require(path.join(DIST_DIR, 'skill-transfer.js'))
})

const SKILL_ID = 'builtin:summary-workbook'

/** One version's files, as the server would ship them. */
function bundle(overrides) {
  const files = {
    'SKILL.md': Buffer.from('---\nname: summary-workbook\ndescription: d\n---\n', 'utf-8'),
    'scripts/summarize.py': Buffer.from("print('summarize')\n", 'utf-8'),
    ...(overrides || {}),
  }
  return Object.entries(files).map(([relativePath, body]) => ({ relativePath, body }))
}

function newCache(limits) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'skill-transfer-'))
  return { cache: new cacheMod.SkillCache(root, limits), root }
}

/** A `fetch` stand-in that records its calls and answers with `payload`. */
function stubFetch(payload, { status = 200 } = {}) {
  const calls = []
  const fn = async (url, init) => {
    calls.push({ url, init })
    return {
      ok: status >= 200 && status < 300,
      status,
      text: async () => JSON.stringify(payload),
    }
  }
  fn.calls = calls
  return fn
}

function pullRequest(overrides) {
  return {
    origin: 'https://console.test',
    nativeBearer: 'native-token',
    commandId: 'cmd_1',
    deviceId: 'dev_1',
    bindingId: 'bind_1',
    workspaceId: 'ws_1',
    grantVersion: 1,
    paramsDigest: 'sha256:' + 'a'.repeat(64),
    skillId: SKILL_ID,
    digest: pkg.packageDigest(bundle()),
    ...(overrides || {}),
  }
}

/** The server's answer for one package, as the endpoint sends it. */
function answered(entries, overrides) {
  return {
    skill_id: SKILL_ID,
    digest: pkg.packageDigest(entries),
    resources: [],
    entries: entries.map((entry) => ({
      relative_path: entry.relativePath,
      body_base64: entry.body.toString('base64'),
    })),
    ...(overrides || {}),
  }
}

// -- fetchSkillPackage ------------------------------------------------------

test('the request names the command and carries the native bearer', async () => {
  const entries = bundle()
  const fetchFn = stubFetch(answered(entries))

  const result = await transfer.fetchSkillPackage(pullRequest({ fetchFn }))

  assert.equal(result.ok, true)
  const [{ url, init }] = fetchFn.calls
  assert.match(url, /^https:\/\/console\.test\/api\/desktop\/execution\/skill-package\?/)
  const query = new URL(url).searchParams
  assert.equal(query.get('command_id'), 'cmd_1')
  assert.equal(query.get('device_id'), 'dev_1')
  assert.equal(query.get('binding_id'), 'bind_1')
  assert.equal(query.get('workspace_id'), 'ws_1')
  assert.equal(query.get('grant_version'), '1')
  assert.equal(query.get('params_digest'), 'sha256:' + 'a'.repeat(64))
  assert.equal(query.get('skill_id'), SKILL_ID)
  assert.equal(query.get('digest'), pkg.packageDigest(entries))
  // The credential travels in the header, never in the query: a URL ends up in
  // logs and referrers, and this is the one credential the device holds.
  assert.equal(init.headers.Authorization, 'Bearer native-token')
})

test('the bytes come back as the package they hash to', async () => {
  const entries = bundle()
  const result = await transfer.fetchSkillPackage(
    pullRequest({ fetchFn: stubFetch(answered(entries)) }))

  assert.equal(result.ok, true)
  assert.deepEqual(result.entries, entries)
})

test('an answer for a different digest is refused, not installed', async () => {
  const entries = bundle()
  const other = bundle({ 'SKILL.md': Buffer.from('---\nname: other\n---\n') })

  const result = await transfer.fetchSkillPackage(pullRequest({
    fetchFn: stubFetch(answered(other)),
  }))

  assert.equal(result.ok, false)
  assert.equal(result.code, 'incompatible_skill')
})

test('a refusal from the server keeps the server code', async () => {
  const result = await transfer.fetchSkillPackage(pullRequest({
    fetchFn: stubFetch(
      { status: 'error', code: 'incompatible_skill', message: 'not authorized' },
      { status: 422 }),
  }))

  assert.equal(result.ok, false)
  assert.equal(result.code, 'incompatible_skill')
  assert.equal(result.message, 'not authorized')
})

test('a transport failure is reported instead of retried into silence', async () => {
  const fetchFn = async () => { throw new Error('ECONNREFUSED') }

  const result = await transfer.fetchSkillPackage(pullRequest({ fetchFn }))

  assert.equal(result.ok, false)
  assert.equal(result.code, 'backend_unavailable')
  assert.match(result.message, /ECONNREFUSED/)
})

test('a body that is not base64 is refused rather than decoded loosely', async () => {
  const entries = bundle()
  const payload = answered(entries)
  payload.entries[0].body_base64 = 'not base64 !!!'

  const result = await transfer.fetchSkillPackage(
    pullRequest({ fetchFn: stubFetch(payload) }))

  assert.equal(result.ok, false)
  assert.equal(result.code, 'incompatible_skill')
})

test('an incomplete request never reaches the network', async () => {
  const fetchFn = stubFetch(answered(bundle()))

  const result = await transfer.fetchSkillPackage(pullRequest({
    fetchFn, deviceId: '',
  }))

  assert.equal(result.ok, false)
  assert.equal(result.code, 'invalid_request')
  assert.equal(fetchFn.calls.length, 0)
})

// -- ensureSkillVersions ----------------------------------------------------

test('a version already held is not fetched again', async () => {
  const entries = bundle()
  const digest = pkg.packageDigest(entries)
  const { cache } = newCache()
  cache.install(SKILL_ID, digest, entries)
  const fetchFn = stubFetch(answered(entries))

  const result = await transfer.ensureSkillVersions({
    cache, declared: [{ skillId: SKILL_ID, digest }], ...pullRequest({ fetchFn }),
  })

  assert.equal(result.ok, true)
  assert.equal(fetchFn.calls.length, 0)
})

test('a missing version is fetched, installed and then resolvable', async () => {
  const entries = bundle()
  const digest = pkg.packageDigest(entries)
  const { cache } = newCache()
  const fetchFn = stubFetch(answered(entries))

  const result = await transfer.ensureSkillVersions({
    cache, declared: [{ skillId: SKILL_ID, digest }], ...pullRequest({ fetchFn }),
  })

  assert.equal(result.ok, true)
  assert.equal(fetchFn.calls.length, 1)
  // Resolvable is the point: the fetch only matters because the run's own gate
  // (``resolveDeclared``) reads the cache afterwards.
  assert.deepEqual(cache.resolveDeclared([{ skillId: SKILL_ID, digest }]),
                   [cache.versionDir(SKILL_ID, digest)])
})

test('a package whose bytes do not match the declared digest is refused', async () => {
  const declared = pkg.packageDigest(bundle())
  const other = bundle({ 'scripts/summarize.py': Buffer.from('print("swapped")\n') })
  const { cache } = newCache()

  const result = await transfer.ensureSkillVersions({
    cache,
    declared: [{ skillId: SKILL_ID, digest: declared }],
    // The answer *names* the requested digest but carries different bytes: a
    // server, or a hop before it, that lies about which version it is shipping.
    // The transport's digest echo agrees with the request here, so only the
    // cache's own recomputation can catch it -- which is the case this check
    // exists for.
    ...pullRequest({ fetchFn: stubFetch(answered(other, { digest: declared })) }),
  })

  assert.equal(result.ok, false)
  assert.equal(result.code, 'incompatible_skill')
  // And nothing was left behind for a later run to mistake for the version.
  assert.equal(cache.has(SKILL_ID, declared), false)
  assert.throws(() => cache.resolveDeclared([{ skillId: SKILL_ID, digest: declared }]),
                /incompatible_skill/)
})

test('a missing cache is refused without pretending there is nothing to fetch', async () => {
  const fetchFn = stubFetch(answered(bundle()))

  const result = await transfer.ensureSkillVersions({
    cache: null,
    declared: [{ skillId: SKILL_ID, digest: pkg.packageDigest(bundle()) }],
    ...pullRequest({ fetchFn }),
  })

  assert.equal(result.ok, false)
  assert.equal(result.code, 'skill_cache_unavailable')
  assert.equal(fetchFn.calls.length, 0)
})

test('one failure stops the delivery rather than half-installing a set', async () => {
  const good = bundle()
  const goodDigest = pkg.packageDigest(good)
  const declared = [
    { skillId: 'builtin:a', digest: goodDigest },
    { skillId: 'builtin:b', digest: goodDigest },
  ]
  let call = 0
  const fetchFn = async () => {
    call += 1
    if (call === 1) {
      return { ok: true, status: 200, text: async () => JSON.stringify(answered(good)) }
    }
    return {
      ok: false, status: 422,
      text: async () => JSON.stringify({ code: 'incompatible_skill', message: 'no' }),
    }
  }
  const { cache } = newCache()

  const result = await transfer.ensureSkillVersions({
    cache, declared, ...pullRequest({ fetchFn }),
  })

  assert.equal(result.ok, false)
  assert.equal(result.code, 'incompatible_skill')
  assert.equal(call, 2)
  // The second skill is genuinely absent -- the run must not proceed on a partial
  // set, and ``resolveDeclared`` is what says so.
  assert.throws(() => cache.resolveDeclared(declared), /incompatible_skill/)
})

test('a run with no declared skills makes no request at all', async () => {
  const fetchFn = stubFetch(answered(bundle()))
  const { cache } = newCache()

  const result = await transfer.ensureSkillVersions({
    cache, declared: [], ...pullRequest({ fetchFn }),
  })

  assert.equal(result.ok, true)
  assert.equal(fetchFn.calls.length, 0)
})
