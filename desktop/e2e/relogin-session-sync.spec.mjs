// The desktop re-login journey, in the real app (change
// `fix-desktop-relogin-session-sync`, tasks 4.1/4.2).
//
// Run:
//   node desktop/e2e/run-remote-workbench.mjs --spec ./relogin-session-sync.spec.mjs
//
// What is real here: the shipped Electron main process, the real console page the
// app carries in a container view, the real native broker, the product's own
// sign-out entries (`handleLogout` and `desktopRelogin`), the real `signOut`
// bridge method, the real `detachRemoteContainer` and the real grant registry.
// The single scripted thing is the native sheet's outcome, exactly as in
// `directory-selection.spec.mjs` -- see the dialog block in `main.e2e.cjs`.
//
// The bug being reproduced, in the order it happened on 2026-10-01: signing out
// of a container used to end only the Web half and then reload the page into the
// ordinary password form. The page looked signed in again, but the native host
// held no session, so the next directory bind answered "not signed in". The
// claims below are therefore *about the ends*, not about pixels:
//
//   * after a sign-out the container is gone and the native broker holds no
//     session -- there is no page left that can look signed in;
//   * the local shell is the only UI left, and it offers its own login again;
//   * signing in again and picking a *new* directory really reads that
//     directory, and the previous authorization does not come back;
//   * a container's login screen offers the recovery entry, and a password
//     submit there cannot mint the Web-only session the split state needs.

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
const DIALOG_CALLS = path.join(PROFILE, 'dialog-calls.jsonl')
// Picked directories live inside the run's private profile, so nothing this
// spec creates can be confused with the operator's own files.
const PICK_ROOT = path.join(PROFILE, 'relogin-picks')

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
let shell = null

/**
 * One plain-HTTP request with a cookie jar.
 *
 * The bundled local backend is `http://localhost:<port>`, so the browser leg of
 * the sign-in is plain HTTP here -- nothing is being tunnelled and nothing is
 * being trusted that the product does not already trust (the app registered this
 * exact origin with its own broker).
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
 * loopback listener. The consent page alone mints nothing, which is the property
 * the flow rests on.
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
  return jar
}

/**
 * Sign in to the bundled local backend, the way the app offers.
 *
 * The click is delivered to the React shell's own button element. This is the
 * same entry a user presses after a sign-out -- which is the point of the
 * journey: the shell, not a Web page, is where a container comes back from.
 */
async function signIn(target, account = LOCAL_USER) {
  const gate = target.getByRole('button', { name: LABEL_LOGIN })
  await gate.waitFor({ timeout: 90000 })
  // The button needs the broker's own readiness, not merely the gate: a click in
  // the window between the two is answered with "backend is not ready".
  await waitFor('the identity broker to reach the backend', () =>
    target.evaluate(() => window.electronAPI.desktopAuthProbe()
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
  await completeBrowserLeg(url, { ...account, origin })
  await waitFor('the local session to be accepted', () =>
    target.evaluate(() => !/在浏览器中登录|Sign in with browser/.test(document.body.innerText)))

  // A first sign-in on a fresh profile opens the onboarding wizard. Dismiss it
  // the way the wizard offers -- "skip" -- so the shell underneath is the state
  // a user would see. Best effort: the container may already be covering the
  // shell by now, and the console this journey drives does not depend on it.
  try {
    await target.getByRole('button', { name: LABEL_SKIP }).first().click({ timeout: 15000 })
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
  shell = await app.firstWindow()
  await shell.waitForLoadState('domcontentloaded')
  // The native session lives in the broker's memory, so a restart needs a new
  // sign-in. Detected rather than assumed: an already-attached console means this
  // launch inherited one.
  if (!(await containerInfo()).attached) await signIn(shell)
  return app
}

// ---------------------------------------------------------------------------
// main-process authority

const grantRegistry = () => app.evaluate(() => globalThis.__cowE2E.grantRegistry())
const containerInfo = () => app.evaluate(() => globalThis.__cowE2E.remoteContainerInfo())

/** Run a snippet in the container's real page. */
const remote = (code) => app.evaluate((_electron, snippet) => globalThis.__cowE2E.remoteEval(snippet), code)

/** What the native host believes about the account, asked of the local shell. */
const brokerStatus = () => shell.evaluate(() => window.electronAPI.desktopAuthStatus())

/** Wait until the app exposes a console page that can ask for a directory. */
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

/** Every dialog the app opened. */
function dialogOpens() {
  if (!fs.existsSync(DIALOG_CALLS)) return []
  return fs
    .readFileSync(DIALOG_CALLS, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((line) => JSON.parse(line))
    .filter((entry) => entry.kind === 'call')
}

// ---------------------------------------------------------------------------
// the page surfaces this journey asserts on

/** The composer chip's label: the project the user currently has selected. */
const chipLabel = () =>
  remote(`(() => {
    const label = document.getElementById('workspace-selector-label');
    return label ? label.textContent : null;
  })()`)

/** The project the panel currently has open (its live binding), or null. */
const projectBinding = () =>
  remote(`(() => {
    if (typeof CowProjectSource === 'undefined') return null;
    const binding = CowProjectSource.binding();
    return binding ? { workspace_id: binding.workspace_id, label: binding.label } : null;
  })()`)

/** Run the product's own selection entry point, awaiting the whole flow. */
const chooseLocalDir = () => remote('window.wsSelChooseLocalDir()')

/**
 * Read a project-relative file through the product's own local-project surface.
 *
 * The `workspace_id` is taken from the panel's own live binding -- the same one
 * its file tree reads through -- so this asks the host for the project the user
 * actually selected, with the grant that selection produced. Returns the raw
 * bridge reply, so a refusal is reported as the refusal it was.
 *
 * The reply's own shape is the panel's: the narrow preload hands back the
 * payload the host wrapped (`remote-preload.ts` returns `reply.data`), so a
 * successful read is the browser's `{ ok, text, encoding, bytes, mtime }` and a
 * refusal is `{ ok: false, code, message }`. Both are returned as they are.
 */
const readProjectFile = (relative) =>
  remote(`(async () => {
    const binding = CowProjectSource.binding();
    if (!binding) return { ok: false, code: 'no_binding', message: 'no local project is bound' };
    try {
      return await CowDesktopHost.project('projectRead', {
        workspace_id: String(binding.workspace_id), path: ${JSON.stringify(relative)}
      });
    } catch (error) {
      return { ok: false, code: 'threw', message: String(error && error.message || error) };
    }
  })()`)

const mkPickDir = (name) => {
  const dir = path.join(PICK_ROOT, name)
  fs.mkdirSync(dir, { recursive: true })
  return dir
}

/** A file inside a picked directory, so the read has something real to return. */
function mkPickFile(dir, name, contents) {
  fs.writeFileSync(path.join(dir, name), contents)
  return name
}

/** Pick `name` and wait for the console to show it, returning the grant rows. */
async function pickAndWait(name) {
  const dir = mkPickDir(name)
  const opens = dialogOpens().length
  scriptDialogs([{ result: { filePaths: [dir] } }])
  await chooseLocalDir()
  await waitFor(`the chip to show ${name}`, async () => (await chipLabel()) === name)
  await waitFor('the dialog to have been opened', () => dialogOpens().length === opens + 1)
  return dir
}

// ---------------------------------------------------------------------------

before(() => {
  fs.rmSync(DIALOG_CALLS, { force: true })
  fs.mkdirSync(PICK_ROOT, { recursive: true })
})

after(async () => {
  if (app) await app.close().catch(() => undefined)
})

test('R01: a container that just signed in can pick a directory and really read it', async () => {
  await launch()
  const info = await waitForConsole()
  console.log(`[e2e] console page: ${info.url}`)
  assert.equal(info.attached, true, 'the container must be attached for the console flow to run')
  await waitForConsoleWorkbench()

  // The premise of every claim below: this page really is a container, and this
  // build really declares the sign-out the change added.
  assert.equal(await remote('CowDesktopAccount.isDesktop()'), true,
    'the console must recognise the container it is running in')
  assert.equal(await remote('CowDesktopAccount.canSignOut()'), true,
    'this build must declare signOut, or the recovery entry would only offer an upgrade')

  const alpha = await pickAndWait('alpha')
  const note = mkPickFile(alpha, 'note.txt', 'alpha-note\n')
  // The directory the *next* case will pick, prepared in advance so its contents
  // are not confused with this one's.
  mkPickFile(mkPickDir('beta'), 'note.txt', 'beta-note\n')

  // The read is what makes the pick mean something: same account, same grant,
  // real bytes back through the real host.
  const read = await readProjectFile(note)
  assert.equal(read.ok, true, `reading the picked file was refused: ${JSON.stringify(read)}`)
  assert.equal(read.text, 'alpha-note\n',
    'the file served must be the one in the directory the user picked')
})

test('R02: the account menu ends both ends, and leaves no page that looks signed in', async () => {
  const before = await grantRegistry()
  assert.equal(before.length, 1, 'the precondition of this case is one live authorization')

  // The original repro: the user opens the account menu and signs out. The page
  // is destroyed by the host mid-call, so the attempt is started and not awaited
  // -- a page that outlives its own sign-out would be the bug.
  await remote('(handleLogout(), true)')

  await waitFor('the host to tear the container down', async () => !(await containerInfo()).attached,
    { timeout: 60000 })

  // There is no page left, so there is nothing left that can render a signed-in
  // shell over a host that has no session -- which is exactly the split state the
  // old reload produced.
  assert.equal((await containerInfo()).attached, false)

  // The old authorization does not survive the sign-out.
  const after = await grantRegistry()
  assert.deepEqual(after, [], 'a signed-out host must not keep the previous directory authorized')

  // The native half really ended, asked of the broker itself rather than inferred
  // from the container's absence.
  const status = await waitFor('the broker to report no session', async () => {
    const reply = await brokerStatus()
    return reply && reply.ok && !reply.session && !reply.blockedReason ? reply : null
  }, { timeout: 60000 })
  assert.equal(status.session, null, 'the native session must be gone, not merely hidden')
  assert.ok(!status.blockedReason, `the sign-out must be confirmed, not blocked: ${status.blockedReason}`)

  // The local shell is the only UI left, and it offers its own login again.
  await shell.getByRole('button', { name: LABEL_LOGIN }).waitFor({ timeout: 60000 })
})

test('R03: signing in again picks a new directory, and the old authorization does not return', async () => {
  await signIn(shell)
  const info = await waitForConsole()
  assert.equal(info.attached, true, 'the shell login must bring the container back')
  await waitForConsoleWorkbench()

  const beta = await pickAndWait('beta')

  const grants = await grantRegistry()
  assert.equal(grants.length, 1, `exactly the new pick must be authorized, got ${JSON.stringify(grants)}`)
  assert.equal(grants[0].absolutePath, beta, 'the new pick must be the authorization in effect')
  assert.ok(!grants.some((grant) => grant.label === 'alpha'),
    'the directory authorized before the sign-out must not come back with the new session')

  // And the new binding really reads the new directory.
  const read = await readProjectFile('note.txt')
  assert.equal(read.ok, true, `reading the new project was refused: ${JSON.stringify(read)}`)
  assert.equal(read.text, 'beta-note\n',
    'the read must come from the newly picked directory, not the previous one')
})

test('R04: the recovery entry ends the account, and a password submit there cannot mint a Web-only session', async () => {
  // The login screen a container draws. The product's own function, so what is
  // asserted is the decision this build makes -- not a mock of it.
  await remote('(showLoginScreen(), true)')

  const drawn = await waitFor('the login screen to be drawn', () =>
    remote(`(() => {
      const form = document.getElementById('login-form');
      const recovery = document.getElementById('login-recovery');
      if (!form || !recovery) return null;
      const hidden = (el) => el.classList.contains('hidden');
      return { formHidden: hidden(form), recoveryHidden: hidden(recovery),
               recoveryAction: !!document.getElementById('login-recovery-btn') };
    })()`), { timeout: 30000 })

  assert.equal(drawn.recoveryHidden, false,
    'a container must be offered the recovery entry instead of the password form')
  assert.equal(drawn.formHidden, true,
    'the password form must be hidden: it is the Web-only login this change removes')
  assert.equal(drawn.recoveryAction, true, 'the recovery entry must carry its action')

  // The guard behind that rendering: a password submit in a container is refused,
  // so the Web-only session cannot be created even by a caller that tries.
  const refused = await remote(`(async () => {
    const username = document.getElementById('login-username');
    const password = document.getElementById('login-password');
    if (password) password.value = 'whatever';
    if (username) username.value = ${JSON.stringify(LOCAL_USER.username)};
    const before = { authRequired: _accountState.authRequired, authenticated: _accountState.authenticated };
    const result = await _submitAccountLogin({ preventDefault: function () {} });
    return { result: result, before: before, phase: _accountState.phase };
  })()`)
  assert.equal(refused.result, false, 'a password login in a container must not be accepted')
  assert.equal(refused.phase, 'unauthenticated',
    'the page must stay unauthenticated rather than adopting a Web-only identity')

  // Now the entry itself: it ends the Web half and then the native half, which is
  // what lets the shell present its own login over a host that is signed out.
  await remote('(desktopRelogin(), true)')

  await waitFor('the host to tear the container down from the recovery entry',
    async () => !(await containerInfo()).attached, { timeout: 60000 })
  const after = await grantRegistry()
  assert.deepEqual(after, [], 'the recovery entry must also drop the previous authorization')

  const status = await waitFor('the broker to report no session', async () => {
    const reply = await brokerStatus()
    return reply && reply.ok && !reply.session && !reply.blockedReason ? reply : null
  }, { timeout: 60000 })
  assert.equal(status.session, null)

  await shell.getByRole('button', { name: LABEL_LOGIN }).waitFor({ timeout: 60000 })
})

test('R05: after two sign-outs and two sign-ins the workbench is still usable', async () => {
  // The claim the change is *for*: this is a recovery path, not a one-way door.
  // A third session proves nothing was consumed by the first two.
  const gamma = mkPickDir('gamma')
  const note = mkPickFile(gamma, 'note.txt', 'gamma-note\n')

  await signIn(shell)
  await waitForConsole()
  await waitForConsoleWorkbench()

  await pickAndWait('gamma')
  const read = await readProjectFile(note)
  assert.equal(read.ok, true, `the third session could not read its project: ${JSON.stringify(read)}`)
  assert.equal(read.text, 'gamma-note\n',
    'gamma must serve gamma: the same file name in a different directory is a different file')
})

test('R06: changing accounts does not carry the previous authorization over', async () => {
  const SECOND = JSON.parse(process.env.COW_E2E_LOCAL_SECOND || 'null')
  assert.ok(SECOND, 'the fixture must seed a second local account for this case')

  // The previous case ended signed in as the first account with `gamma`
  // authorized. That is the starting state this case changes accounts from.
  const before = await grantRegistry()
  assert.equal(before.length, 1, 'the precondition of this case is one live authorization')
  const previousPath = before[0].absolutePath

  await remote('(handleLogout(), true)')
  await waitFor('the host to tear the container down', async () => !(await containerInfo()).attached,
    { timeout: 60000 })
  assert.deepEqual(await grantRegistry(), [],
    'the first account must own nothing once it has signed out')

  // A different account signs in on the same machine, with no restart.
  await signIn(shell, SECOND)
  const info = await waitForConsole()
  assert.equal(info.attached, true, 'the second account must reach a workbench of its own')
  await waitForConsoleWorkbench()

  // The authorization the first account held must not be visible to, or
  // usable by, the second one: no grant survives, and the previous account's
  // project is not the project the new account has open.
  assert.deepEqual(await grantRegistry(), [],
    'a different account must not inherit the previous directory authorization')
  const inherited = await projectBinding()
  assert.equal(inherited, null,
    `the previous account's project must not come back for a new account, got ${JSON.stringify(inherited)}`)

  // The second account picks and reads its *own* directory, so what is
  // asserted is a working flow for the new account and not merely an empty
  // registry: same file name, different directory, different bytes.
  const delta = mkPickDir('delta')
  const note = mkPickFile(delta, 'note.txt', 'delta-note\n')
  await pickAndWait('delta')
  const grants = await grantRegistry()
  assert.equal(grants.length, 1, `exactly the new pick must be authorized, got ${JSON.stringify(grants)}`)
  assert.equal(grants[0].absolutePath, delta, 'the new account must own the directory it picked')
  assert.notEqual(grants[0].absolutePath, previousPath,
    'the new account must not be reading the directory the previous account picked')
  const read = await readProjectFile(note)
  assert.equal(read.ok, true, `the second account could not read its project: ${JSON.stringify(read)}`)
  assert.equal(read.text, 'delta-note\n',
    'the second account must be served its own file')

  // And the second account can still end its own session: the account
  // lifecycle does not depend on the file slice.
  await remote('(handleLogout(), true)')
  await waitFor('the host to tear the second container down',
    async () => !(await containerInfo()).attached, { timeout: 60000 })
  const status = await waitFor('the broker to report no session', async () => {
    const reply = await brokerStatus()
    return reply && reply.ok && !reply.session && !reply.blockedReason ? reply : null
  }, { timeout: 60000 })
  assert.equal(status.session, null)
  await shell.getByRole('button', { name: LABEL_LOGIN }).waitFor({ timeout: 60000 })
})
