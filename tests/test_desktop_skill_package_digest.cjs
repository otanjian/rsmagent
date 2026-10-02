// Run: node --test tests/test_desktop_skill_package_digest.cjs
//
// Task 8.9: the device verifies a skill package against the digest the server
// declared. That only works if both ends compute the *same* digest from the same
// bytes, so this file does not re-derive an expected value in JavaScript -- it
// asks the real Python implementation and compares. A hand-written expectation
// here would pass while the two implementations disagreed, which is exactly the
// failure this test exists to catch.
//
// The Python side is the definition (`agent/desktop_local/package_rules.py` and
// `agent/skills/manifest.py`); `skill-package.ts` is a port and must not drift.

const test = require('node:test')
const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { execFileSync } = require('node:child_process')

const ROOT = path.join(__dirname, '..')
const DESKTOP = path.join(ROOT, 'desktop')
const SRC_DIR = path.join(DESKTOP, 'src', 'main', 'project-execution')
const DIST_DIR = path.join(DESKTOP, 'dist', 'main', 'project-execution')
const PY = path.join(ROOT, '.venv', 'bin', 'python')

let mod

function needsBuild() {
  const compiled = path.join(DIST_DIR, 'skill-package.js')
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
  mod = require(path.join(DIST_DIR, 'skill-package.js'))
})

function python(script) {
  return execFileSync(PY, ['-c', script], { cwd: ROOT, encoding: 'utf-8' }).trim()
}

/** A Python snippet with the repo root already on `sys.path`. */
function prelude(lines) {
  return ['import sys', `sys.path.insert(0, ${JSON.stringify(ROOT)})`, ...lines].join('\n')
}

/**
 * A package written to a temp dir, in the order given, so the Python walker and
 * the JS entry list describe the same bytes.
 */
function writePackage(entries) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'skill-pkg-'))
  for (const [relative, body] of entries) {
    const full = path.join(dir, ...relative.split('/'))
    fs.mkdirSync(path.dirname(full), { recursive: true })
    fs.writeFileSync(full, body)
  }
  return dir
}

function pythonManifestDigest(dir) {
  // Through the real manifest builder, so the digest tested is the one the
  // server actually declares -- not a re-implementation of it in the test.
  return python(prelude([
    'from agent.skills.manifest import _walk_resources, _content_digest',
    `resources = _walk_resources(${JSON.stringify(dir)})`,
    'print(_content_digest(resources))',
  ]))
}

function pythonEntryDigests(dir) {
  return JSON.parse(python(prelude([
    'import json',
    'from agent.skills.manifest import _walk_resources',
    'print(json.dumps([',
    '  {"path": r.relative_path, "digest": r.digest, "size": r.size}',
    `  for r in _walk_resources(${JSON.stringify(dir)})]))`,
  ])))
}

function entriesFrom(dir, relatives) {
  return relatives.map((relative) => ({
    relativePath: relative,
    body: fs.readFileSync(path.join(dir, ...relative.split('/'))),
  }))
}

const BUNDLE = [
  ['SKILL.md', Buffer.from('---\nname: excel\ndescription: d\n---\n', 'utf-8')],
  ['templates/report.xlsx', Buffer.from('template-bytes')],
  ['scripts/fill.py', Buffer.from("print('fill')\n", 'utf-8')],
]

test('the package digest is the one the real Python manifest computes', () => {
  const dir = writePackage(BUNDLE)
  const expected = pythonManifestDigest(dir)

  assert.equal(mod.packageDigest(entriesFrom(dir, BUNDLE.map((e) => e[0]))), expected)
})

test('per-file digests and sizes match the Python walker', () => {
  const dir = writePackage(BUNDLE)
  const relatives = BUNDLE.map((e) => e[0])
  const expected = pythonEntryDigests(dir)

  const resources = mod.packageResources(entriesFrom(dir, relatives))

  assert.deepEqual(
    resources.map((r) => ({ path: r.relativePath, digest: r.digest, size: r.size })),
    expected)
})

test('entry order does not change the digest', () => {
  const dir = writePackage(BUNDLE)
  const relatives = BUNDLE.map((e) => e[0])
  const forward = mod.packageDigest(entriesFrom(dir, relatives))
  const backward = mod.packageDigest(entriesFrom(dir, [...relatives].reverse()))

  assert.equal(forward, backward)
})

test('a renamed file changes the digest even when the bytes do not', () => {
  const dir = writePackage(BUNDLE)
  const relatives = BUNDLE.map((e) => e[0])
  const before = mod.packageDigest(entriesFrom(dir, relatives))

  const renamed = entriesFrom(dir, relatives)
  renamed[2] = { relativePath: 'scripts/other.py', body: renamed[2].body }

  assert.notEqual(mod.packageDigest(renamed), before)
})

test('an added, removed or edited file changes the digest', () => {
  const dir = writePackage(BUNDLE)
  const relatives = BUNDLE.map((e) => e[0])
  const base = entriesFrom(dir, relatives)
  const before = mod.packageDigest(base)

  const added = [...base, { relativePath: 'extra.txt', body: Buffer.from('x') }]
  assert.notEqual(mod.packageDigest(added), before, '添加文件后摘要未变')

  assert.notEqual(mod.packageDigest(base.slice(0, 2)), before, '删除文件后摘要未变')

  const edited = [...base]
  edited[0] = { relativePath: edited[0].relativePath, body: Buffer.from('changed') }
  assert.notEqual(mod.packageDigest(edited), before, '修改文件后摘要未变')
})

test('an empty package has a defined digest but cannot become a manifest', () => {
  // Two different questions, and the Python side answers them in two places:
  // `_content_digest` is total (an empty set has a digest, and both ends must
  // agree on it), while `build_skill_manifest` refuses a skill with no resources
  // at all. Asserting the *pair* pins that the device does not invent a refusal
  // the server does not make, nor accept a package the server would never build.
  assert.equal(mod.packageDigest([]), mod.contentDigest([]))
  assert.match(mod.packageDigest([]), /^sha256:[0-9a-f]{64}$/)

  const dir = writePackage([])
  assert.equal(mod.packageDigest([]),
    python(prelude([
      'from agent.skills.manifest import _walk_resources, _content_digest',
      `print(_content_digest(_walk_resources(${JSON.stringify(dir)})))`,
    ])))

  // The manifest builder is the one that refuses it, and it refuses by name.
  const verdict = python(prelude([
    'from agent.skills.manifest import build_skill_manifest, SkillManifestError',
    'from agent.skills.types import Skill, SkillEntry',
    `skill = Skill(name="empty", description="d",`,
    `              file_path=${JSON.stringify(path.join(dir, 'SKILL.md'))},`,
    `              base_dir=${JSON.stringify(dir)}, source="builtin", content="", frontmatter={})`,
    'try:',
    '    build_skill_manifest(SkillEntry(skill=skill), platform="posix",',
    '                        is_authorized=lambda _sid: True)',
    '    print("built")',
    'except SkillManifestError as err:',
    '    print("refused:" + err.code)',
  ]))
  assert.equal(verdict, 'refused:skill_unavailable', verdict)
})

test('the two ends refuse the same unsafe paths', () => {
  const refused = ['/etc/passwd', 'C:\\\\Windows\\\\system32', 'a/../b', './a', 'a//b', '']
  for (const raw of refused) {
    assert.throws(() => mod.safeRelative(raw), `${raw} 在设备侧被接受`)
    // The Python side is asked for the same verdict, so "the device is stricter"
    // cannot pass as agreement.
    const verdict = python(prelude([
      'from agent.desktop_local.package_rules import safe_relative, PackageRuleError',
      'try:',
      `    print("ok:" + safe_relative(${JSON.stringify(raw)}))`,
      'except PackageRuleError as err:',
      '    print("refused:" + err.code)',
    ]))
    assert.equal(verdict, 'refused:path_outside_package', `${raw}: ${verdict}`)
  }
})

test('the two ends refuse the same credential names', () => {
  const secrets = ['.env', '.env.local', 'config/id_rsa', 'keys/server.pem',
    'certs/store.p12', '.git/config', '.ssh/known_hosts', 'credentials.json']
  for (const relative of secrets) {
    assert.equal(mod.isSecretName(relative), true, `${relative} 在设备侧未被当成凭据`)
    const verdict = python(prelude([
      'from agent.desktop_local.package_rules import is_secret_name',
      `print("secret" if is_secret_name(${JSON.stringify(relative)}) else "content")`,
    ]))
    assert.equal(verdict, 'secret', `${relative}: ${verdict}`)
  }

  for (const content of ['SKILL.md', 'scripts/fill.py', 'templates/report.xlsx',
    'references/fields.md', 'env.sample']) {
    assert.equal(mod.isSecretName(content), false, `${content} 被误判为凭据`)
    const verdict = python(prelude([
      'from agent.desktop_local.package_rules import is_secret_name',
      `print("secret" if is_secret_name(${JSON.stringify(content)}) else "content")`,
    ]))
    assert.equal(verdict, 'content', `${content}: ${verdict}`)
  }
})

test('a duplicate path is refused instead of silently overwriting', () => {
  const dir = writePackage(BUNDLE)
  const entries = entriesFrom(dir, ['SKILL.md', 'SKILL.md'])

  assert.throws(() => mod.packageResources(entries), /duplicate_resource/)
})

test('the declared digest a device compares against is content, not metadata', () => {
  // Two packages with identical bytes are the same version even if they arrived
  // at different times or under different temp names -- otherwise a redelivery
  // would look like a new version and be re-transferred forever.
  const dir = writePackage(BUNDLE)
  const relatives = BUNDLE.map((e) => e[0])
  const first = mod.packageDigest(entriesFrom(dir, relatives))

  const other = fs.mkdtempSync(path.join(os.tmpdir(), 'skill-pkg-again-'))
  for (const [relative, body] of BUNDLE) {
    const full = path.join(other, ...relative.split('/'))
    fs.mkdirSync(path.dirname(full), { recursive: true })
    fs.writeFileSync(full, body)
  }

  assert.equal(mod.packageDigest(entriesFrom(other, relatives)), first)
  assert.equal(crypto.createHash('sha256').update(Buffer.from('x')).digest('hex').length, 64)
})
