// Runner for the remote-workbench Electron E2E (change task 5.8).
//
// It does four things, in the order they have to happen:
//
//   1. resolves the Playwright module (this repo does not vendor it, so the
//      spec is handed an absolute path rather than being allowed to guess);
//   2. builds the *local*-mode identity database the first leg signs in to --
//      the shipped app in local mode needs an account of its own, and it must
//      not be the operator's (see ``seed_local_identity`` in serve-fixture.py);
//   3. starts ``serve-fixture.py`` -- the *real* console over a private identity
//      database on a real HTTPS socket -- and waits for its readiness line;
//   4. hands the fixture's origin, certificate, control origin and the local
//      credentials to the spec through the environment and runs it.
//
// Nothing here asserts anything: every claim in the report comes from the spec,
// from the product's own code, or from the fixture's printed readiness line.
//
//   node desktop/e2e/run-remote-workbench.mjs [--keep]
//
// Exit code is the spec's; a non-zero run means the E2E failed.

import { execFileSync, spawn } from 'node:child_process'
import { createRequire } from 'node:module'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const HERE = path.dirname(fileURLToPath(import.meta.url))
const DESKTOP = path.join(HERE, '..')
const REPO = path.join(DESKTOP, '..')
const PYTHON = path.join(REPO, '.venv', 'bin', 'python')
const ELECTRON = path.join(DESKTOP, 'node_modules', '.bin', 'electron')

const argv = process.argv.slice(2)
const keep = argv.includes('--keep')
/**
 * Which spec file to run. Defaults to the remote-workbench journey; any other
 * journey over the same fixture is named explicitly, so a run always says which
 * set of claims it is about to make:
 *
 *   node desktop/e2e/run-remote-workbench.mjs --spec ./directory-selection.spec.mjs
 */
const specIndex = argv.indexOf('--spec')
const specFile = specIndex >= 0 ? argv[specIndex + 1] : './remote-workbench.spec.mjs'
if (specIndex >= 0 && !specFile) throw new Error('--spec needs a file path')

/** Find Playwright: a local install first, then the machine's global one. */
function resolvePlaywright() {
  const candidates = ['playwright', 'playwright-core']
  for (const name of candidates) {
    try {
      return require.resolve(name)
    } catch {
      /* not installed here */
    }
  }
  try {
    const globalRoot = require('node:child_process')
      .execFileSync('npm', ['root', '-g'], { encoding: 'utf8' })
      .trim()
    return require.resolve(path.join(globalRoot, 'playwright'))
  } catch {
    return ''
  }
}

/**
 * Find the ``fs-guard`` helper, the one thing a local *read* needs.
 *
 * The app resolves it from ``app.getAppPath()``, which for this harness is
 * ``desktop/e2e`` -- the directory of ``main.e2e.cjs`` -- so none of the
 * app-relative candidates exist and every ``projectRead`` is answered
 * ``the local file helper is not available``. That refusal is indistinguishable
 * from a build that ships without the helper, so a journey whose claims include
 * "the bytes really came back" would be asserting against a feature gap instead
 * of against the product. ``COW_FS_GUARD`` is the app's own documented override
 * for exactly this, and resolving it here keeps the read path real rather than
 * mocked.
 *
 * Returns ``''`` when no helper is built; the caller reports that as a missing
 * precondition rather than letting each spec discover it as a refusal.
 */
function resolveGuardHelper() {
  const candidates = [
    path.join(DESKTOP, 'native', 'fs-guard', 'target', 'release', 'fs-guard'),
    path.join(DESKTOP, 'native', 'fs-guard', 'target', 'debug', 'fs-guard'),
  ]
  for (const candidate of candidates) {
    try {
      fs.accessSync(candidate, fs.constants.X_OK)
      return candidate
    } catch {
      /* not built at this profile */
    }
  }
  return ''
}

/**
 * Build the local-mode identity database the first app launch signs in to.
 *
 * ``--seed-local-identity`` reuses the fixture's own bootstrap (a real tenant
 * with a real root administrator) in the run's private directory; nothing is
 * read from the operator's installation.
 */
function seedLocalIdentity(dataDir) {
  const out = execFileSync(PYTHON, [
    path.join(HERE, 'serve-fixture.py'),
    '--data-dir', dataDir,
    '--seed-local-identity', dataDir,
  ], { cwd: REPO, encoding: 'utf8' })
  const lines = out.trim().split('\n')
  const parsed = JSON.parse(lines[lines.length - 1])
  return parsed.local_identity
}

/**
 * Kill Electron processes left behind by a previous launch of this harness.
 *
 * Playwright closes the app it launched when `app.close()` runs, but a launch
 * that *times out* never yields an `app`, so nothing closes it: the spec's own
 * cleanup is `if (app) await app.close()`, which is a no-op precisely when the
 * launch failed. The leftovers keep the bundled backend's fixed port bound and
 * burn CPU, so the next run fails for a reason that has nothing to do with it.
 *
 * The match is deliberately narrow -- the **executable** must be Electron *and*
 * `e2e/main.e2e.cjs` must be one of its arguments. Matching substrings of the
 * whole command line instead would kill the shell that launched this runner
 * (whose own command line quotes these very strings) and any `grep` for them.
 */
function reapStrayElectron() {
  let listing = ''
  try {
    listing = execFileSync('ps', ['-Ao', 'pid=,command='], { encoding: 'utf8' })
  } catch {
    return 0
  }
  let killed = 0
  for (const line of listing.split('\n')) {
    const match = /^\s*(\d+)\s+(.*)$/.exec(line)
    if (!match) continue
    const pid = Number(match[1])
    const tokens = match[2].split(/\s+/)
    const binary = tokens[0] ?? ''
    const isElectron = /node_modules[/\\]electron[/\\]/.test(binary)
      || /Electron(\.app)?\//.test(binary)
    if (!isElectron) continue
    if (!tokens.some((token) => token.endsWith('e2e/main.e2e.cjs'))) continue
    // Never the caller, and never its parent shell.
    if (pid === process.pid || pid === process.ppid) continue
    try {
      process.kill(pid, 'SIGKILL')
      killed += 1
    } catch {
      /* already gone */
    }
  }
  return killed
}

function startFixture(profileDir) {
  const consoleDir = path.join(profileDir, 'console')
  const logPath = path.join(profileDir, 'fixture.log')
  const logStream = fs.createWriteStream(logPath, { flags: 'a' })
  const child = spawn(PYTHON, [
    path.join(HERE, 'serve-fixture.py'),
    '--data-dir', consoleDir,
    '--port', '0',
    '--model-port', '0',
    '--control-port', '0',
  ], { cwd: REPO, stdio: ['ignore', 'pipe', 'pipe'] })

  let buffered = ''
  /** Set while the runner is stopping the fixture on purpose. */
  let stopping = false
  /** Everything the fixture has said, so a failure can quote it. */
  const log = () => buffered
  // Registered before the readiness wait, not after it. A fixture that dies in
  // the moment between printing its readiness line and being awaited would
  // otherwise emit its only ``exit`` before any listener existed -- the run then
  // looks like "the server stopped answering" with no cause recorded anywhere.
  child.on('exit', (code, signal) => {
    if (stopping) return
    console.error(
      `[e2e] the fixture exited (code=${code} signal=${signal}); log: ${logPath}\n${buffered}`,
    )
  })
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`the fixture did not become ready:\n${buffered}`)), 180000)
    const onData = (chunk) => {
      logStream.write(chunk)
      buffered += chunk.toString()
      // Whichever stream it arrived on, take the first line that is the
      // readiness record -- pytest-style warnings on stderr must not be able to
      // hide it.
      for (const line of buffered.split('\n')) {
        try {
          const parsed = JSON.parse(line)
          if (parsed && parsed.ready && parsed.origin) {
            clearTimeout(timer)
            resolve(parsed)
            return
          }
        } catch {
          /* not the readiness line */
        }
      }
    }
    child.stdout.on('data', onData)
    child.stderr.on('data', onData)
    child.on('exit', (code) => reject(new Error(`the fixture exited with ${code}:\n${buffered}`)))
  })
  ready.catch(() => undefined)
  return {
    child,
    ready,
    log,
    logPath,
    stop() {
      stopping = true
      child.kill('SIGKILL')
    },
  }
}

async function main() {
  if (!fs.existsSync(PYTHON)) throw new Error(`no interpreter at ${PYTHON}`)
  if (!fs.existsSync(ELECTRON)) throw new Error(`no electron at ${ELECTRON} (run npm install in desktop/)`)

  // Reap *before* the fixture starts, not only after. A leftover app keeps the
  // bundled backend's fixed port bound, so this run's app would attach its
  // container to the leftover's backend and every claim made about "the"
  // session would be about a different process. Reaping only in `after()` is
  // one run too late for the run it poisons, which reads as the product
  // hanging at sign-out rather than as a dirty machine.
  const reapedAtStart = reapStrayElectron()
  if (reapedAtStart) {
    console.log(`[e2e] reaped ${reapedAtStart} stray Electron process(es) before starting`)
  }

  // The app is launched from its *built* output -- `main.e2e.cjs` requires
  // `dist/main/index.js` and the window loads `dist/renderer/index.html` -- but
  // this runner never builds it. A missing build is therefore a *missing
  // precondition*, not a test result, and it has to be reported as one: without
  // this check the symptom is `electron.launch` timing out after 180s with only
  // "ws connected" in its call log, which reads as the product hanging and sends
  // the next person hunting through window-creation code. Cost of getting it
  // wrong: a whole afternoon.
  const builtMain = path.join(DESKTOP, 'dist', 'main', 'index.js')
  const builtRenderer = path.join(DESKTOP, 'dist', 'renderer', 'index.html')
  const missing = [
    [builtMain, 'build:main'],
    [builtRenderer, 'build:renderer'],
  ].filter(([file]) => !fs.existsSync(file))
  if (missing.length) {
    const hint = missing.map(
      ([file, script]) => `  missing ${path.relative(REPO, file)} -- run \`npm run ${script}\` in desktop/`,
    )
    throw new Error(
      `the app has not been built, so there is nothing for Electron to load:\n${hint.join('\n')}`,
    )
  }

  const playwrightPath = resolvePlaywright()
  if (!playwrightPath) {
    throw new Error('Playwright was not found. Install it (`npm i -D playwright`) or make it available globally.')
  }

  const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cow-e2e-profile-'))
  // The bundled backend's data dir: the app and the backend must agree on it, so
  // it is exported once, here, and inherited by every launch.
  const localDataDir = path.join(profileDir, 'cow')
  const localUser = seedLocalIdentity(localDataDir)

  const fixture = startFixture(profileDir)
  let info
  try {
    info = await fixture.ready
  } catch (error) {
    fixture.child.kill('SIGKILL')
    throw error
  }

  console.log(`[e2e] fixture ready: ${info.origin}`)
  console.log(`[e2e] profile: ${profileDir}`)
  console.log(`[e2e] local data dir: ${localDataDir}`)

  process.env.COW_E2E_PLAYWRIGHT = playwrightPath
  process.env.COW_E2E_ELECTRON = ELECTRON
  process.env.COW_E2E_MAIN = path.join(HERE, 'main.e2e.cjs')
  process.env.COW_E2E_PROFILE_DIR = profileDir
  process.env.COW_E2E_ORIGIN = info.origin
  process.env.COW_E2E_CONTROL = info.control_origin
  process.env.COW_E2E_CERT = info.tls_cert
  process.env.COW_E2E_SPKI = info.tls_spki
  process.env.COW_E2E_USERS = JSON.stringify(info.users)
  process.env.COW_E2E_SECOND_TENANT = JSON.stringify(info.second_tenant)
  process.env.COW_E2E_TENANT_ID = info.tenant_id
  process.env.COW_E2E_LOCAL_USER = JSON.stringify(localUser)
  // The second local account, so a journey can *change* accounts rather than
  // only re-enter as the same user.
  process.env.COW_E2E_LOCAL_SECOND = JSON.stringify(localUser.second_user || null)
  process.env.COW_E2E_LOCAL_DATA_DIR = localDataDir
  // The helper a local *read* needs. The app resolves it from `app.getAppPath()`
  // -- `desktop/e2e` under this harness -- so none of its own candidates exist
  // here and every read would be answered `the local file helper is not
  // available`, a refusal indistinguishable from a build that ships without the
  // helper. `COW_FS_GUARD` is the app's own override for exactly this, and using
  // it keeps the read path real instead of mocked. Absence is a precondition,
  // reported before anything is launched rather than discovered as a refusal.
  const guardHelper = resolveGuardHelper()
  if (!guardHelper) {
    throw new Error(
      'the fs-guard helper is not built, so no local read can be served:\n'
      + '  run `cargo build --release` in desktop/native/fs-guard',
    )
  }
  process.env.COW_FS_GUARD = guardHelper
  console.log(`[e2e] fs-guard helper: ${guardHelper}`)
  // The native broker's own TLS: Node verifies the real chain, against the
  // fixture's CA. Not a certificate-error override.
  process.env.NODE_EXTRA_CA_CERTS = info.tls_cert
  // Point the bundled backend at the run's private directory. Without this the
  // source-mode app reads the checkout's config.json and identity.db -- the
  // operator's real ones -- and both the port and the account would be theirs.
  process.env.COW_DATA_DIR = localDataDir

  // ``node:test`` *registers* the tests when the spec is imported and *runs*
  // them only once the module has finished evaluating. Tearing the fixture down
  // here therefore killed the server before the first test that needs it -- the
  // symptom was "the connection shell never stored the origin" followed by a
  // cascade of ECONNREFUSED, with the fixture's own log holding nothing but its
  // readiness line. The teardown is registered as the last ``after`` hook
  // instead, which is the point at which every test in the file has settled.
  //
  // COW_E2E_SPEC exists so a narrower spec file can be pointed at the same
  // fixture while diagnosing a failure; the shipped run uses the default.
  const { after } = await import('node:test')
  console.log(`[e2e] spec: ${specFile}`)
  try {
    await import(process.env.COW_E2E_SPEC || specFile)
  } catch (error) {
    // The spec could not even be loaded: no test will run, so nothing else will
    // stop the fixture.
    fixture.stop()
    throw error
  }
  after(() => {
    if (!keep) fixture.stop()
    else console.log(`[e2e] --keep: fixture still running at ${info.origin}`)
    const reaped = reapStrayElectron()
    if (reaped) console.log(`[e2e] reaped ${reaped} stray Electron process(es) from a previous launch`)
  })
}

main().catch((error) => {
  console.error('[e2e] runner failed:', error && error.stack ? error.stack : error)
  // A run that threw may still have left an app behind (see reapStrayElectron).
  reapStrayElectron()
  process.exitCode = 1
})
