// Run: node --test tests/test_desktop_skill_cache.cjs
//
// Task 8.9, device half: the cache that makes "this run must use this version"
// enforceable. The failure it exists to prevent is quiet -- a device with a stale
// snapshot running the version it happens to hold and reporting success -- so the
// tests below are mostly about what the cache *refuses*.

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
const PY = path.join(ROOT, '.venv', 'bin', 'python')

let pkg
let cacheMod

function needsBuild() {
  const compiled = path.join(DIST_DIR, 'skill-cache.js')
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
})

const SKILL_ID = 'builtin:summary-workbook'

/** The package a device would receive, in the shape the transfer delivers it. */
function bundle(overrides) {
  const files = {
    'SKILL.md': Buffer.from('---\nname: summary-workbook\ndescription: d\n---\n', 'utf-8'),
    'scripts/summarize.py': Buffer.from("print('summarize')\n", 'utf-8'),
    'templates/summary.xlsx': Buffer.from('template-bytes'),
    ...(overrides || {}),
  }
  return Object.entries(files).map(([relativePath, body]) => ({ relativePath, body }))
}

function digestOf(entries) {
  return pkg.packageDigest(entries)
}

function newCache(limits) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'skill-cache-'))
  return new cacheMod.SkillCache(root, limits)
}

test('an installed version is present and complete', () => {
  const cache = newCache()
  const entries = bundle()
  const digest = digestOf(entries)

  const dir = cache.install(SKILL_ID, digest, entries)

  assert.equal(cache.has(SKILL_ID, digest), true)
  assert.equal(fs.existsSync(path.join(dir, 'SKILL.md')), true)
  assert.equal(dir.startsWith(cache.root + path.sep), true, '缓存目录逃出了根')
})

test('a version is addressed by digest, so two versions never share a directory', () => {
  const cache = newCache()
  const first = bundle()
  const second = bundle({ 'scripts/summarize.py': Buffer.from("print('v2')\n", 'utf-8') })

  const a = cache.install(SKILL_ID, digestOf(first), first)
  const b = cache.install(SKILL_ID, digestOf(second), second)

  assert.notEqual(a, b)
  assert.equal(cache.has(SKILL_ID, digestOf(first)), true)
  assert.equal(cache.has(SKILL_ID, digestOf(second)), true)
  assert.equal(cache.listVersions(SKILL_ID).length, 2)
})

test('a package whose content does not match the declared digest is refused', () => {
  const cache = newCache()
  const entries = bundle()

  assert.throws(
    () => cache.install(SKILL_ID, 'sha256:' + '0'.repeat(64), entries),
    (err) => err.code === 'incompatible_skill' && /摘要不符/.test(err.message))
})

test('a refused install leaves nothing that a later run could mistake for a version', () => {
  const cache = newCache()
  const declared = digestOf(bundle())
  const tampered = bundle({ 'templates/summary.xlsx': Buffer.from('tampered') })

  assert.throws(() => cache.install(SKILL_ID, declared, tampered))

  assert.equal(cache.has(SKILL_ID, declared), false)
  // No staging directory survives either: an inert `.partial-*` would grow the
  // cache forever and, worse, could be renamed into place by a later bug.
  const skillDir = path.join(cache.root, cacheMod.skillComponent(SKILL_ID))
  const leftovers = fs.existsSync(skillDir)
    ? fs.readdirSync(skillDir).filter((name) => name.startsWith('.partial-'))
    : []
  assert.deepEqual(leftovers, [])
})

test('the declared set resolves to read-only roots, or refuses by name', () => {
  const cache = newCache()
  const entries = bundle()
  const digest = digestOf(entries)
  cache.install(SKILL_ID, digest, entries)

  assert.deepEqual(cache.resolveDeclared([{ skillId: SKILL_ID, digest }]),
    [cache.versionDir(SKILL_ID, digest)])

  assert.throws(
    () => cache.resolveDeclared([{ skillId: SKILL_ID, digest: 'sha256:' + 'a'.repeat(64) }]),
    (err) => err.code === 'incompatible_skill')
})

test('a missing version is refused instead of falling back to a same-named skill', () => {
  // The core requirement. Another version of the *same skill* is present, and
  // that must not satisfy the declaration: running it would produce a plausible
  // result for a package the run was never authorized with.
  const cache = newCache()
  const stale = bundle({ 'scripts/summarize.py': Buffer.from("print('stale')\n", 'utf-8') })
  const staleDigest = digestOf(stale)
  cache.install(SKILL_ID, staleDigest, stale)

  const wanted = digestOf(bundle())

  assert.throws(
    () => cache.resolveDeclared([{ skillId: SKILL_ID, digest: wanted }]),
    (err) => err.code === 'incompatible_skill' && err.message.includes(wanted))
  assert.equal(cache.has(SKILL_ID, staleDigest), true, '旧版本被顺手删掉了')
})

test('an empty declaration resolves to no roots', () => {
  const cache = newCache()
  assert.deepEqual(cache.resolveDeclared([]), [])
})

test('a malformed declared digest is refused, not ignored', () => {
  const cache = newCache()
  for (const bad of ['', 'sha256:short', 'md5:' + 'a'.repeat(32), null, undefined]) {
    assert.throws(() => cache.resolveDeclared([{ skillId: SKILL_ID, digest: bad }]),
      (err) => err.code === 'incompatible_skill', `accepted: ${String(bad)}`)
  }
})

test('a directory without SKILL.md does not count as an installed version', () => {
  // "The bytes are here" and "this is a skill the worker can run" are different
  // claims; only the second lets the worker start.
  const cache = newCache()
  const entries = bundle()
  const digest = digestOf(entries)
  const dir = cache.versionDir(SKILL_ID, digest)
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(path.join(dir, 'scripts.py'), 'x')

  assert.equal(cache.has(SKILL_ID, digest), false)
  assert.throws(() => cache.resolveDeclared([{ skillId: SKILL_ID, digest }]),
    (err) => err.code === 'incompatible_skill')
})

test('installing the same version twice is idempotent', () => {
  const cache = newCache()
  const entries = bundle()
  const digest = digestOf(entries)

  const first = cache.install(SKILL_ID, digest, entries)
  const second = cache.install(SKILL_ID, digest, entries)

  assert.equal(first, second)
  assert.equal(cache.listVersions(SKILL_ID).length, 1)
})

test('a package with an unsafe path is refused before anything is written', () => {
  const cache = newCache()
  const hostile = bundle({ '../escape.py': Buffer.from('x') })
  const digest = pkg.packageDigest(bundle())

  assert.throws(() => cache.install(SKILL_ID, digest, hostile),
    (err) => err.code === 'path_outside_package')
  assert.equal(fs.existsSync(path.join(cache.root, cacheMod.skillComponent(SKILL_ID))), false)
})

test('a package carrying a credential is refused', () => {
  const cache = newCache()
  const withSecret = bundle({ '.env': Buffer.from('TOKEN=1') })
  const digest = pkg.packageDigest(bundle())

  assert.throws(() => cache.install(SKILL_ID, digest, withSecret),
    (err) => err.code === 'secret_refused')
})

test('the contract package budgets are enforced by the cache itself', () => {
  // A device that trusted the sender's accounting would be enforcing nothing, so
  // the limits are checked where the whole package is visible.
  const fileLimited = newCache({ filesMax: 2, expandedMaxBytes: 4096 })
  assert.throws(() => fileLimited.install(SKILL_ID, digestOf(bundle()), bundle()),
    (err) => err.code === 'limit_exceeded' && /文件数/.test(err.message))

  // A separate cache: with the file ceiling in force the byte ceiling would
  // never be reached, so one cache cannot test both.
  const byteLimited = newCache({ filesMax: 1000, expandedMaxBytes: 1024 })
  const big = bundle({ 'payload.bin': Buffer.alloc(2048) })
  assert.throws(() => byteLimited.install(SKILL_ID, digestOf(big), big),
    (err) => err.code === 'limit_exceeded' && /展开后/.test(err.message))
})

test('a skill id cannot escape the cache root', () => {
  const cache = newCache()
  const entries = bundle()
  const digest = digestOf(entries)

  const dir = cache.install('../../etc/passwd', digest, entries)

  assert.equal(dir.startsWith(cache.root + path.sep), true, dir)
  assert.equal(fs.existsSync(path.join(cache.root, '..', '..', 'etc')), false)
})

test('a version the real Python published is accepted by the device cache', () => {
  // The two ends only agree if the digest algorithm agrees, so this drives the
  // Python publish path and feeds the result to the TypeScript cache. A digest
  // that matched only in a JavaScript re-derivation would fail here.
  const published = JSON.parse(execFileSync(PY, ['-c', [
    'import sys, json, os, tempfile, hashlib, base64',
    `sys.path.insert(0, ${JSON.stringify(ROOT)})`,
    'from agent.desktop_local.skill_cache import SkillCache, SkillScope',
    'from agent.skills.manifest import build_skill_manifest, read_skill_payloads',
    'from agent.skills.types import Skill, SkillEntry',
    'root = tempfile.mkdtemp()',
    'base = os.path.join(root, "authoring", "summary-workbook")',
    'bundle = {"SKILL.md": b"---\\nname: summary-workbook\\ndescription: d\\n---\\n",',
    '          "scripts/summarize.py": b"print(\'summarize\')\\n",',
    '          "templates/summary.xlsx": b"template-bytes"}',
    'for rel, body in bundle.items():',
    '    full = os.path.join(base, *rel.split("/"))',
    '    os.makedirs(os.path.dirname(full), exist_ok=True)',
    '    open(full, "wb").write(body)',
    'skill = Skill(name="summary-workbook", description="d",',
    '              file_path=os.path.join(base, "SKILL.md"), base_dir=base,',
    '              source="builtin", content=bundle["SKILL.md"].decode(), frontmatter={})',
    'manifest = build_skill_manifest(SkillEntry(skill=skill), platform="posix",',
    '                                is_authorized=lambda _s: True)',
    'cache = SkillCache(os.path.join(root, "cache"))',
    'scope = SkillScope(origin="https://master.test", tenant_id="t1", user_id="u1")',
    'cache.publish(scope, {',
    '    "skill_id": manifest.skill_id, "digest": manifest.digest,',
    '    "platform": manifest.platform,',
    '    "resources": [{"relative_path": r.relative_path, "digest": r.digest,',
    '                   "size": r.size} for r in manifest.resources]},',
    '    read_skill_payloads(manifest))',
    'print(json.dumps({"digest": manifest.digest, "entries": [',
    '    {"relativePath": k, "body": base64.b64encode(v).decode()}',
    '    for k, v in read_skill_payloads(manifest).items()]}))',
  ].join('\n')], { cwd: ROOT, encoding: 'utf-8' }).trim())

  const entries = published.entries.map((entry) => ({
    relativePath: entry.relativePath,
    body: Buffer.from(entry.body, 'base64'),
  }))

  const cache = newCache()
  const dir = cache.install(SKILL_ID, published.digest, entries)

  assert.equal(cache.has(SKILL_ID, published.digest), true)
  assert.equal(fs.readFileSync(path.join(dir, 'SKILL.md'), 'utf-8'),
    '---\nname: summary-workbook\ndescription: d\n---\n')
  // The installed files are readable but not writable by their owner, so a
  // sandboxed worker cannot edit the version it is running.
  const mode = fs.statSync(path.join(dir, 'SKILL.md')).mode & 0o777
  assert.equal(mode & 0o200, 0, 'cached skill 文件对属主可写')
})
