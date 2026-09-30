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
  process.env.COW_E2E_LOCAL_DATA_DIR = localDataDir
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
  try {
    await import(process.env.COW_E2E_SPEC || './remote-workbench.spec.mjs')
  } catch (error) {
    // The spec could not even be loaded: no test will run, so nothing else will
    // stop the fixture.
    fixture.stop()
    throw error
  }
  after(() => {
    if (!keep) fixture.stop()
    else console.log(`[e2e] --keep: fixture still running at ${info.origin}`)
  })
}

main().catch((error) => {
  console.error('[e2e] runner failed:', error && error.stack ? error.stack : error)
  process.exitCode = 1
})
