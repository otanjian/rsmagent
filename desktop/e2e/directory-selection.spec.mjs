// The native directory-selection journey, in the real app (change
// `align-desktop-project-execution-with-master`, task 2.5 / acceptance A01+A02).
//
// Run:
//   node desktop/e2e/run-remote-workbench.mjs --spec ./directory-selection.spec.mjs
//
// What is real here: the shipped Electron main process, the real console page the
// app loads in a container view, the product's own `wsSelChooseLocalDir()` entry
// point, the real `chooseWorkspace` handler, the real selection service and the
// real grant registry. What is scripted is the one thing an automated test cannot
// own -- the native sheet's two OS-owned decisions (whether a result comes back,
// and how long the user takes); see the dialog block in `main.e2e.cjs`. So "the
// user cancels" and "the user picks B while still holding the dialog for A" are
// reproductions of a user's timing, not stand-ins for the logic under test.
//
// Every assertion is made against either the page's own visible state or the
// authorization table itself, both read back through the product.
//
// How the console is reached, recorded rather than hidden: the app boots in
// local mode, the spec performs the *product's own* browser sign-in (`app ->
// system browser -> loopback callback`, the PKCE exchange the app ships), and
// then the app auto-binds the local backend's console into its container view
// (`remote/local-web-bind.ts`). Everything after that -- the console page, the
// `wsSelChooseLocalDir()` entry, `chooseWorkspace`, the selection service, the
// grant registry -- is the product's own code. The React shell's *Settings*
// journey is not walked: once the local console is attached it covers the
// shell (`TOP_INSET` 0, `pointer-events: none`), so a shell button is not
// reachable by a user either -- `remote-workbench.spec.mjs` fails there, and
// that is recorded in `evidence/` as a stale harness assumption, not papered
// over here.

import { createRequire } from 'node:module'
import assert from 'node:assert/strict'
import { test, before, after } from 'node:test'
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { URL } from 'node:url'

const require = createRequire(import.meta.url)
const { _electron: electron } = require(process.env.COW_E2E_PLAYWRIGHT)

const ELECTRON_BIN = process.env.COW_E2E_ELECTRON
const MAIN_E2E = process.env.COW_E2E_MAIN
const PROFILE = process.env.COW_E2E_PROFILE_DIR
const CERT = process.env.COW_E2E_CERT
const SPKI = process.env.COW_E2E_SPKI
const LOCAL_USER = JSON.parse(process.env.COW_E2E_LOCAL_USER)
const OPENED_EXTERNAL = path.join(PROFILE, 'opened-external.jsonl')

// Sign-in labels, matched by accessible name in either shipped language.
const LABEL_LOGIN = /在浏览器中登录|Sign in with browser/
const LABEL_SKIP = /^(跳过|Skip)$/

const DIALOG_SCRIPT = path.join(PROFILE, 'dialog-script.json')
const DIALOG_RELEASES = path.join(PROFILE, 'dialog-releases.json')
const DIALOG_CALLS = path.join(PROFILE, 'dialog-calls.jsonl')
// The picked directories live inside the run's private profile, so nothing this
// spec creates can be confused with the operator's own files.
const PICK_ROOT = path.join(PROFILE, 'picks')

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function waitFor(what, fn, { timeout = 45000, interval = 250 } = {}) {
  const deadline = Date.now() + timeout
  let last = null
  for (;;) {
    try {
      const value = await fn()
      if (value) return value
      last = value
    } catch (error) {
      last = error && error.message ? error.message : String(error)
    }
    if (Date.now() > deadline) {
      throw new Error(`timed out waiting for ${what}; last=${JSON.stringify(last)}`)
    }
    await sleep(interval)
  }
}

let app = null

/**
 * One plain-HTTP request with a cookie jar.
 *
 * The bundled local backend is `http://localhost:<port>`, so the browser leg of
 * the sign-in is plain HTTP here -- nothing is being tunnelled and nothing is
 * being trusted that the product does not already trust (the app registered
 * this exact origin with its own broker).
 */
function request(url, { method = 'GET', headers = {}, body = null, jar = null } = {}) {
  const target = new URL(url)
  const outgoing = { headers }
  if (jar && jar.size) outgoing.Cookie = [...jar.entries()].map(([k, v]) => `${k}=${v}`).join('; ')
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        protocol: target.protocol,
        hostname: target.hostname,
        port: target.port,
        path: target.pathname + target.search,
        method,
        headers: outgoing,
      },
      (res) => {
        for (const cookie of res.headers['set-cookie'] || []) {
          const [pair] = cookie.split(';')
          const eq = pair.indexOf('=')
          if (eq > 0) jar?.set(pair.slice(0, eq).trim(), pair.slice(eq + 1).trim())
        }
        let data = ''
        res.setEncoding('utf8')
        res.on('data', (chunk) => { data += chunk })
        res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: data }))
      },
    )
    req.on('error', reject)
    if (body !== null) req.write(body)
    req.end()
  })
}

/** Every URL the app handed to the system browser. */
function openedExternal() {
  try {
    return fs
      .readFileSync(OPENED_EXTERNAL, 'utf8')
      .split('\n')
      .filter(Boolean)
      .map((line) => JSON.parse(line).url)
  } catch {
    return []
  }
}

/** Authorize URLs already consumed, so a re-run never replays an old one. */
const authorizeUrlSeen = new Set()

/**
 * The browser leg of the sign-in, exactly as a browser performs it: sign in,
 * GET the consent page, POST the consent, then deliver the code to the app's
 * loopback listener. The consent page alone mints nothing, which is the
 * property the flow rests on.
 */
async function completeBrowserLeg(authorizeUrl, { username, password, origin }) {
  const jar = new Map()
  const login = await request(`${origin}/auth/login`, {
    method: 'POST',
    jar,
    headers: { 'Content-Type': 'application/json', Origin: origin },
    body: JSON.stringify({ username, password }),
  })
  assert.equal(login.status, 200, `sign-in failed: ${login.body}`)
  assert.ok(jar.has('cow_session'), 'the console did not set a session cookie')

  const consent = await request(authorizeUrl, { jar })
  assert.equal(consent.status, 200, `consent page failed: ${consent.status}`)
  const requestId = /name="request_id"[^>]*value="([^"]+)"/.exec(consent.body)
    || /value="([^"]+)"[^>]*name="request_id"/.exec(consent.body)
  const csrf = /name="csrf"[^>]*value="([^"]+)"/.exec(consent.body)
    || /value="([^"]+)"[^>]*name="csrf"/.exec(consent.body)
  assert.ok(requestId, 'the consent page carried no request id')
  assert.ok(csrf, 'the consent page carried no csrf token')

  const form = new URLSearchParams({ request_id: requestId[1], csrf: csrf[1], decision: 'allow' }).toString()
  const confirmed = await request(`${origin}/auth/desktop/authorize`, {
    method: 'POST',
    jar,
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', Origin: origin },
    body: form,
  })
  const location = confirmed.headers.location
  assert.ok(location, `the consent POST did not redirect: ${confirmed.status}`)
  await request(location, { jar }).catch(() => undefined)
}

/**
 * Sign in to the bundled local backend, the way the app offers.
 *
 * The click is delivered to the React shell's own button element: at this point
 * nothing covers the shell (the container is attached *after* a session exists),
 * so this is the same button a user presses. Kept in the spec because the
 * A01/A02 journey needs a real console page, and a console only exists after a
 * real session.
 */
async function signIn(shell) {
  const gate = shell.getByRole('button', { name: LABEL_LOGIN })
  await gate.waitFor({ timeout: 90000 })
  // The button needs the broker's own readiness, not merely the gate: a click in
  // the window between the two is answered with "backend is not ready".
  await waitFor('the identity broker to reach the backend', () =>
    shell.evaluate(() => window.electronAPI.desktopAuthProbe()
      .then((reply) => !!reply && reply.ok === true)
      .catch(() => false)))
  await gate.click()

  const url = await waitFor('the authorize URL', () => {
    const found = openedExternal().find((candidate) => candidate.includes('/auth/desktop/authorize')
      && !authorizeUrlSeen.has(candidate))
    return found || null
  }, { timeout: 60000 })
  authorizeUrlSeen.add(url)
  const origin = new URL(url).origin
  assert.match(origin, /^http:\/\/(localhost|127\.0\.0\.1|\[::1\]):\d+$/,
    `the bundled backend must announce a loopback origin, got ${origin}`)
  await completeBrowserLeg(url, { ...LOCAL_USER, origin })
  await waitFor('the local session to be accepted', () =>
    shell.evaluate(() => !/在浏览器中登录|Sign in with browser/.test(document.body.innerText)))

  // A first sign-in on a fresh profile opens the onboarding wizard. Dismiss it
  // the way the wizard offers -- "skip" -- so the shell underneath is the state
  // a user would see. Best effort: the container may already be covering the
  // shell by now, and the console this journey drives does not depend on it.
  try {
    await shell.getByRole('button', { name: LABEL_SKIP }).first().click({ timeout: 15000 })
  } catch {
    /* no wizard, or a covered shell */
  }
}

/** Launch the shipped app and reach a real console page in its container. */
async function launch() {
  app = await electron.launch({
    executablePath: ELECTRON_BIN,
    args: [MAIN_E2E],
    env: {
      ...process.env,
      HOME: PROFILE,
      COW_E2E_PROFILE_DIR: PROFILE,
      COW_E2E_SPKI: SPKI,
      NODE_EXTRA_CA_CERTS: CERT,
    },
  })
  const shell = await app.firstWindow()
  await shell.waitForLoadState('domcontentloaded')
  // The native session lives in the broker's memory, so a restart needs a new
  // sign-in. Detected rather than assumed: an already-attached console means
  // this launch inherited one.
  if (!(await containerInfo()).attached) await signIn(shell)
  return app
}

// ---------------------------------------------------------------------------
// main-process authority
//
// The grant registry is what decides whether a local root may be read, and a page
// is deliberately unable to enumerate it. Reading it from the main process -- the
// same module instance the app loaded -- is what lets this spec state "cancelling
// created no authorization" as a fact about the authorization table, rather than
// an inference from a label.

const grantRegistry = () => app.evaluate(() => globalThis.__cowE2E.grantRegistry())

/** The app's own installation-identity path and current contents. */
const installationIdentity = () => app.evaluate(() => globalThis.__cowE2E.installationIdentity())

/** Clear the installation identity the way a support reset would. */
const resetInstallationIdentity = () => app.evaluate(() => globalThis.__cowE2E.resetInstallationIdentity())

const containerInfo = () => app.evaluate(() => globalThis.__cowE2E.remoteContainerInfo())

/** Run a snippet in the container's real page. */
const remote = (code) => app.evaluate((_electron, snippet) => globalThis.__cowE2E.remoteEval(snippet), code)

/**
 * Wait until the app exposes a console page that can ask for a directory.
 *
 * Deliberately the console's entry point and not the React shell's: the native
 * selection flow under test lives in the console (`wsSelChooseLocalDir` →
 * `chooseWorkspace` → `bindContext`), and the app carries it in a container view.
 * Waiting for *that* page keeps the journey about the selection flow instead of
 * about the shell's own startup choreography (see the scope note in the header).
 */
async function waitForConsole() {
  return waitFor('the console page to expose the directory entry', async () => {
    const state = await containerInfo()
    if (!state.attached) return null
    const ready = await remote('typeof window.wsSelChooseLocalDir === "function"')
    return ready ? state : null
  }, { timeout: 90000 })
}

/**
 * Wait until the console has actually settled on an account.
 *
 * `wsSelChooseLocalDir` needs the tenant and the account (the grant scope is
 * `server/user/tenant/device`); before that it can only answer "unavailable".
 * A multi-tenant account is asked to choose with the console's own picker, and
 * the choice is made on the picker's own control -- the same select and button
 * a user drives.
 */
async function waitForConsoleWorkbench() {
  const settled = await waitFor('the console to settle on its account', () =>
    remote(`(() => {
      const group = document.getElementById('login-tenant-group');
      const app = document.getElementById('app');
      const overlay = document.getElementById('login-overlay');
      if (group && !group.classList.contains('hidden')) {
        const select = document.getElementById('login-tenant-select');
        const values = select ? [...select.options].map((option) => option.value) : [];
        return values.length ? { picker: values } : null;
      }
      if (app && !app.classList.contains('hidden')
          && overlay && overlay.classList.contains('hidden')
          && sessionStorage.getItem('cow_tenant_id')) {
        return { picker: null };
      }
      return null;
    })()`), { timeout: 90000 })

  if (settled.picker) {
    await remote(`(() => {
      const select = document.getElementById('login-tenant-select');
      select.value = ${JSON.stringify(settled.picker[0])};
      document.getElementById('login-btn').click();
      return true;
    })()`)
  }

  await waitFor('the workbench to open in the console', () =>
    remote(`(() => {
      const app = document.getElementById('app');
      const overlay = document.getElementById('login-overlay');
      return !!(app && !app.classList.contains('hidden')
        && overlay && overlay.classList.contains('hidden')
        && sessionStorage.getItem('cow_tenant_id'));
    })()`), { timeout: 60000 })
}

// ---------------------------------------------------------------------------
// dialog scripting

/** Write the scripted dialog outcomes for the next calls, in order. */
function scriptDialogs(steps) {
  fs.writeFileSync(DIALOG_SCRIPT, JSON.stringify(steps))
}

/** Let a held dialog answer. */
function releaseDialog(id) {
  const current = fs.existsSync(DIALOG_RELEASES)
    ? JSON.parse(fs.readFileSync(DIALOG_RELEASES, 'utf8'))
    : []
  fs.writeFileSync(DIALOG_RELEASES, JSON.stringify([...current, id]))
}

/** Every dialog the app opened, and every answer this harness gave it. */
function dialogCalls() {
  if (!fs.existsSync(DIALOG_CALLS)) return []
  return fs
    .readFileSync(DIALOG_CALLS, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((line) => JSON.parse(line))
}

/** Dialog *invocations*: how many times the app asked the user. */
const dialogOpens = () => dialogCalls().filter((entry) => entry.kind === 'call')

// ---------------------------------------------------------------------------
// the page surfaces this journey asserts on

/** The composer chip's label: the project the user currently has selected. */
const chipLabel = () =>
  remote(`(() => {
    const label = document.getElementById('workspace-selector-label');
    return label ? label.textContent : null;
  })()`)

/** The last toast the selection flow showed, if it is still on screen. */
const toastText = () =>
  remote(`(() => {
    const el = document.getElementById('ws-sel-toast');
    if (!el || !el.textContent) return '';
    return el.style.opacity === '1' ? el.textContent : '';
  })()`)

/** Run the product's own selection entry point, awaiting the whole flow. */
const chooseLocalDir = () => remote('window.wsSelChooseLocalDir()')

/**
 * Start the same entry point without waiting for it.
 *
 * The product's handler is an async function and `executeJavaScript` resolves the
 * Promise it returns, so awaiting it would make "hold the dialog open" impossible
 * to express. Returning a plain value lets the caller decide when to collect the
 * attempt -- which is exactly what the stale-callback case turns on.
 */
const startChooseLocalDir = () => remote('(window.wsSelChooseLocalDir(), true)')

/** True while the console offers the native directory entry at all. */
const canChooseWorkspace = () => remote('CowDesktopHost.canChooseWorkspace()')

const mkPickDir = (name) => {
  const dir = path.join(PICK_ROOT, name)
  fs.mkdirSync(dir, { recursive: true })
  return dir
}

// ---------------------------------------------------------------------------

before(async () => {
  fs.rmSync(DIALOG_CALLS, { force: true })
  fs.rmSync(DIALOG_RELEASES, { force: true })
  fs.mkdirSync(PICK_ROOT, { recursive: true })
})

after(async () => {
  if (app) await app.close().catch(() => undefined)
})

test('A01: the real console offers the native directory entry', async () => {
  await launch()
  const info = await waitForConsole()
  console.log(`[e2e] console page: ${info.url}`)
  assert.equal(info.attached, true, 'the container must be attached for the console flow to run')
  await waitForConsoleWorkbench()
  assert.equal(await canChooseWorkspace(), true,
    'the console must expose chooseWorkspace, or nothing below can be exercised')
})

test('A01: with no installation identity, a first pick activates a read-only grant', async () => {
  // "清空有效安装标识后，真实 Electron 首次选目录": the memoised value and the file
  // both go, so the next read is a genuine first run.
  const identityFile = await resetInstallationIdentity()

  const dir = mkPickDir('alpha')
  const before = dialogOpens().length
  scriptDialogs([{ result: { filePaths: [dir] } }])

  await chooseLocalDir()
  await waitFor('the chip to show the picked directory', async () => (await chipLabel()) === 'alpha')

  const opens = dialogOpens()
  assert.equal(opens.length, before + 1, 'exactly one dialog should have been opened')
  const asked = opens[opens.length - 1]
  assert.deepEqual(asked.properties, ['openDirectory'])
  assert.match(asked.message, /只读访问/,
    'the dialog must state the read-only authorization the user is about to make')

  const grants = await grantRegistry()
  assert.equal(grants.length, 1, 'a confirmed pick must create exactly one authorization')
  assert.equal(grants[0].purpose, 'readonly-input', 'a pick without a purpose must stay read-only')
  assert.equal(grants[0].absolutePath, dir)
  assert.equal(grants[0].label, 'alpha')

  // A01's first claim: no `Illegal invocation`. The crypto receiver bug surfaced
  // as exactly that toast instead of a selection.
  const toast = await toastText()
  assert.ok(!/Illegal invocation/.test(toast), `the flow reported a crypto failure: ${toast}`)

  // A first run creates the identity, in the app's own profile directory.
  const identity = await installationIdentity()
  assert.equal(identity.file, identityFile, "the identity path must be the app's own userData")
  assert.ok(identity.raw && identity.raw.length > 0,
    `the main process must persist an installation identity (${identity.file})`)
})

test('A01: cancelling creates no authorization and keeps the selected project', async () => {
  const before = await grantRegistry()
  const beforeChip = await chipLabel()
  const beforeOpens = dialogOpens().length
  scriptDialogs([{ result: { canceled: true } }])

  await chooseLocalDir()
  await waitFor('the dialog to be answered', () => dialogOpens().length === beforeOpens + 1)
  // Nothing is published on cancel, so wait on the authority rather than the UI.
  await sleep(1500)

  const after = await grantRegistry()
  assert.equal(after.length, before.length, 'cancelling must not create an authorization')
  assert.deepEqual(after.map((g) => g.id), before.map((g) => g.id),
    'cancelling must not re-activate or bump any grant')
  // A02's "保留旧项目时 UI 清楚显示旧项目仍生效": the chip still names the directory
  // the user picked before, not the default workspace.
  assert.equal(await chipLabel(), beforeChip)
  assert.equal(await chipLabel(), 'alpha')
})

test('A02: a pick superseded by a newer one cannot publish the older directory', async () => {
  const held = mkPickDir('held')
  const newer = mkPickDir('beta')
  const opensBefore = dialogOpens().length

  // The user opens the dialog for `held` and leaves it open; then opens it again
  // and picks `beta`. The first call is deliberately not awaited -- its Promise
  // lands later, which is the whole hazard. The held step carries a *real*
  // directory, so if the guard were missing this attempt would activate `held`
  // and the assertions below would see it.
  scriptDialogs([{ hold: 'first', result: { filePaths: [held] } }, { result: { filePaths: [newer] } }])
  const stale = startChooseLocalDir()
  await waitFor('the first dialog to be opened and held', () => dialogOpens().length === opensBefore + 1)
  const staleIndex = dialogOpens()[opensBefore].index

  await chooseLocalDir()
  await waitFor('the second dialog to be opened', () => dialogOpens().length === opensBefore + 2)
  await waitFor('the chip to show the newer directory', async () => (await chipLabel()) === 'beta')

  // Now the abandoned dialog finally answers with `held`. It must not commit it.
  releaseDialog('first')
  await stale
  await sleep(2000)

  // The stale attempt really was answered with a directory, so what stopped it was
  // the generation guard and not a dismissal.
  const answered = dialogCalls().find((entry) => entry.kind === 'return' && entry.index === staleIndex)
  assert.ok(answered, 'the held dialog was never answered')
  assert.equal(answered.canceled, false,
    'the stale answer must be a real pick for this case to mean anything')

  assert.equal(await chipLabel(), 'beta',
    'a superseded pick repainted the chip with the older directory')

  const grants = await grantRegistry()
  assert.equal(grants.filter((g) => g.absolutePath === held).length, 0,
    'the superseded pick activated the directory the user had already moved past')
  assert.equal(grants.filter((g) => g.absolutePath === newer).length, 1,
    'the newer pick must be the one in effect')
})

test('A02: a pick whose conversation moved on is not bound to the new session', async () => {
  // A02's other clause: "切换会话或租户后让旧 Promise 返回 … 无错绑". The pick is
  // opened for one conversation and answered only after the user is in another;
  // the console's own `newChat()` is what moves them, so the session id and the
  // selector change exactly as they do for a user.
  const dir = mkPickDir('switched')
  const opensBefore = dialogOpens().length
  const sessionBefore = await remote('sessionId')
  scriptDialogs([{ hold: 'session', result: { filePaths: [dir] } }])

  const stale = startChooseLocalDir()
  await waitFor('the dialog to be opened and held', () => dialogOpens().length === opensBefore + 1)
  const staleIndex = dialogOpens()[opensBefore].index

  await remote('(newChat(), true)')
  const sessionAfter = await waitFor('the console to move to a new session',
    async () => {
      const now = await remote('sessionId')
      return now && now !== sessionBefore ? now : null
    }, { timeout: 30000 })
  assert.notEqual(sessionAfter, sessionBefore, 'the conversation must have moved on')

  releaseDialog('session')
  await stale
  await sleep(2000)

  const answered = dialogCalls().find((entry) => entry.kind === 'return' && entry.index === staleIndex)
  assert.ok(answered && answered.canceled === false,
    'the held dialog must have been answered with a real directory for this case to mean anything')

  // No mis-binding: the new conversation carries no local reference, so the next
  // turn cannot read a directory the user picked for a different conversation.
  assert.equal(await remote('_desktopContextForRequest()'), null,
    'a pick made for an earlier conversation must not be attached to the new one')
  assert.notEqual(await chipLabel(), 'switched',
    'the new conversation must not be labelled with the abandoned pick')
})

test('A01: the installation identity and a second selection survive a restart', async () => {
  const beforeRestart = (await installationIdentity()).raw
  assert.ok(beforeRestart, 'the identity must exist before the restart')

  await app.close()
  app = null
  await launch()
  await waitForConsole()
  await waitForConsoleWorkbench()

  const afterRestart = await installationIdentity()
  assert.equal(afterRestart.raw, beforeRestart,
    'the installation identity must not change across a restart')
  assert.equal(afterRestart.file, (await installationIdentity()).file)

  const dir = mkPickDir('gamma')
  scriptDialogs([{ result: { filePaths: [dir] } }])
  await chooseLocalDir()
  await waitFor('the chip to show the second directory', async () => (await chipLabel()) === 'gamma')

  const grants = await grantRegistry()
  assert.equal(grants.filter((g) => g.absolutePath === dir).length, 1,
    'a second selection after a restart must reach the authority')
})
