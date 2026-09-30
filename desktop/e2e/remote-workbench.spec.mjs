// Electron E2E for the phase-1 remote workbench (change task 5.8).
//
// What this file is
// -----------------
// It drives the *shipped* desktop app -- the same ``dist/main`` the product
// runs, plus ``main.e2e.cjs``'s private profile and pinned test certificate --
// against ``serve-fixture.py``, which is ``build_web_app()`` on a real HTTPS
// socket over a real identity database. Nothing is mocked: the sign-in is the
// product's PKCE exchange, the child session is minted by the product's
// service, the page in the container is the console the server publishes, and
// the chat turn reaches a real OpenAI-compatible upstream.
//
// The journey is the phase-1 one, in order:
//
//   1. a fresh profile boots **local** with the bundled backend, and signs in
//      through the system browser (the same PKCE flow the product ships);
//   2. from Settings the user reaches the connection shell and adds an exact
//      HTTPS server (a plain-HTTP address is refused and never stored);
//   3. entering remote mode authorizes this client against that server and
//      attaches its console in an isolated container;
//   4. the container is driven end to end -- bridge surface, streaming chat,
//      tenant switch, upload, external link, a refused foreign navigation, a
//      denied media request, a server-side revocation, a detach;
//   5. the next launch starts in remote mode **without** the local backend and
//      requires a fresh authorization (nothing is carried over);
//   6. switching back to local mode restores the bundled backend on the next
//      launch.
//
// Two things cannot happen in a test and are replaced at the point a human
// would act, never in the product:
//   * the browser leg of the sign-in (the spec performs the same GET + consent
//     POST a browser does, against the URL the app wrote to disk);
//   * the native "save as" dialog (the spec chooses the destination; the
//     product still writes the bytes).
//
// A ``WebContentsView`` is not a ``BrowserWindow``, so Playwright's
// ``electronApp.windows()`` cannot see the container page. It is reached
// through ``main.e2e.cjs``'s ``remoteEval`` -- real JS in the real page.
//
// Run with: node desktop/e2e/run-remote-workbench.mjs

import { test, after } from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import fs from 'node:fs'
import https from 'node:https'
import http from 'node:http'
import path from 'node:path'
import { URL } from 'node:url'

const require = createRequire(import.meta.url)
const { _electron: electron } = require(process.env.COW_E2E_PLAYWRIGHT)

const ELECTRON_BIN = process.env.COW_E2E_ELECTRON
const MAIN_E2E = process.env.COW_E2E_MAIN
const PROFILE = process.env.COW_E2E_PROFILE_DIR
const ORIGIN = process.env.COW_E2E_ORIGIN
const CONTROL = process.env.COW_E2E_CONTROL
const CERT = process.env.COW_E2E_CERT
const SPKI = process.env.COW_E2E_SPKI
const USERS = JSON.parse(process.env.COW_E2E_USERS)
const SECOND_TENANT = JSON.parse(process.env.COW_E2E_SECOND_TENANT)
const FIRST_TENANT = process.env.COW_E2E_TENANT_ID
const LOCAL_USER = JSON.parse(process.env.COW_E2E_LOCAL_USER)
const CA = fs.readFileSync(CERT)

const OPENED_EXTERNAL = path.join(PROFILE, 'opened-external.jsonl')

// The app's own main-process log. ``initDesktopLogging`` (``index.ts``) mirrors
// every ``console.*`` line into ``$HOME/.cow/run.log``, and the spec passes
// ``HOME=PROFILE``, so this is where this run's launch lines land.
//
// It is read because the app's stdout cannot answer the question: the mode line
// is printed before the window exists, and Electron's early stdout is not
// delivered to ``app.process().stdout`` once Playwright attaches its reader, so
// the line was never in ``bootLog`` and "boots remote" could never pass. This
// file is the product's own record of the same lines, and a byte offset makes
// "what *this* launch said" exact.
const MAIN_LOG = path.join(PROFILE, '.cow', 'run.log')

// Labels of the buttons this spec presses. Matched by role + accessible name so
// a markup change does not silently pass, in both shipped languages.
const LABEL = {
  login: /在浏览器中登录|Sign in with browser/,
  openShell: /打开服务器连接设置|Open server connection settings/,
  checkAndAdd: /检查并添加|Check and add/,
  enterRemote: /进入远程模式|Enter remote mode/,
  backLocal: /切回本地模式|Back to local mode/,
  disconnect: /断开并退出登录|Disconnect and sign out/,
}

// --------------------------------------------------------------------------
// small helpers
// --------------------------------------------------------------------------

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** Poll until ``fn`` returns truthy, or fail with ``what``. */
async function waitFor(what, fn, { timeout = 45000, interval = 250 } = {}) {
  const deadline = Date.now() + timeout
  let last
  while (Date.now() < deadline) {
    last = await fn()
    if (last) return last
    await sleep(interval)
  }
  throw new Error(`timed out waiting for ${what} (last: ${JSON.stringify(last)})`)
}

/** Read the JSON the control plane serves (the fixture's test-only surface). */
async function control(pathname) {
  const res = await fetch(CONTROL + pathname)
  return res.json()
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

/**
 * One HTTP(S) request with a cookie jar.
 *
 * Used only for the browser leg of the sign-in: the app's loopback listener is
 * plain HTTP, the console is on our self-signed HTTPS, and the real chain is
 * still verified against the fixture's certificate.
 */
function request(url, { method = 'GET', headers = {}, body = null, jar = null } = {}) {
  const target = new URL(url)
  const isHttps = target.protocol === 'https:'
  const agent = isHttps ? https : http
  const outgoing = { ...headers }
  if (jar && jar.size) outgoing.Cookie = [...jar.entries()].map(([k, v]) => `${k}=${v}`).join('; ')
  return new Promise((resolve, reject) => {
    const req = agent.request(
      {
        protocol: target.protocol,
        hostname: target.hostname,
        port: target.port,
        path: target.pathname + target.search,
        method,
        headers: outgoing,
        ca: isHttps ? CA : undefined,
        rejectUnauthorized: isHttps,
      },
      (res) => {
        const cookies = res.headers['set-cookie'] || []
        for (const cookie of cookies) {
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

/**
 * The browser leg of the sign-in, exactly as a browser performs it.
 *
 * ``GET /auth/desktop/authorize`` only *renders* a consent page -- it never
 * mints a code by itself -- so the code only appears after the explicit POST.
 * That is the property the flow rests on, and this helper would fail if the
 * server ever changed it.
 */
async function completeBrowserLeg(authorizeUrl, { username, password, origin }) {
  const jar = new Map()
  // No manual Content-Length: the body's length has to match what is actually
  // written, or the server reads a zero-length body and the leftover bytes are
  // parsed as the next request on the same connection.
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
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      Origin: origin,
    },
    body: form,
  })
  const location = confirmed.headers.location
  assert.ok(location, `the consent POST did not redirect: ${confirmed.status} ${confirmed.body.slice(0, 200)}`)
  const code = new URL(location, origin).searchParams.get('code')
  assert.ok(code, `the redirect carried no code: ${location}`)
  // Deliver the code to the app's loopback listener, as the browser would.
  await request(location, { jar }).catch(() => undefined)
  return code
}

// --------------------------------------------------------------------------
// the app
// --------------------------------------------------------------------------

let app = null
let shell = null
let bootLog = []
/** Byte offset in ``MAIN_LOG`` where the current launch starts. */
let launchMark = 0
/** Authorize URLs already consumed, so a re-run never replays an old one. */
const authorizeUrlSeen = new Set()

/** Size of the main-process log, used as a marker for one launch. */
function mainLogBytes() {
  try {
    return fs.statSync(MAIN_LOG).size
  } catch {
    return 0
  }
}

/** The app's own main-process lines for the current launch only. */
function launchedMainLog() {
  try {
    return fs.readFileSync(MAIN_LOG).subarray(launchMark).toString('utf8')
  } catch {
    return ''
  }
}

/**
 * Everything this launch said -- its stdout and its own log file.
 *
 * The two channels carry different lines: Python's forwarded output and the
 * process-level ``Backend ready`` notice reach stdout reliably, while the
 * startup lines the main process prints before the window exists are only
 * complete in ``run.log``.
 */
const launchedLog = () => bootLog.join('') + launchedMainLog()

/**
 * Start the shipped app.
 *
 * ``remote`` says which mode this launch is *expected* to boot in; the local
 * launch is additionally held until the login gate is on screen, which is the
 * only state in which the app accepts a browser authorization.
 */
async function launch({ remote }) {
  // Marked before the process starts, so nothing this launch writes can be
  // confused with the launch before it (the log is append-only across runs).
  launchMark = mainLogBytes()
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
  bootLog = []
  const process_ = app.process()
  process_.stdout?.on('data', (chunk) => bootLog.push(chunk.toString()))
  process_.stderr?.on('data', (chunk) => bootLog.push(chunk.toString()))
  shell = await app.firstWindow()
  await shell.waitForLoadState('domcontentloaded')
  if (remote) {
    await waitFor('the remote-mode boot line', () =>
      launchedLog().includes('[remote] remote mode: local backend not started'))
  } else {
    // The bundled backend has to be up before the shell can offer anything: the
    // gate is the app's own "backend ready and this account is unresolved" state.
    await shell.getByRole('button', { name: LABEL.login }).waitFor({ timeout: 90000 })
  }
  return app
}

/** Restart the app in the other mode, on the same profile. */
async function restart({ remote }) {
  if (app) await app.close().catch(() => undefined)
  app = null
  shell = null
  return launch({ remote })
}

/** Run a snippet in the container's real page. */
const remote = (code) => app.evaluate((_electron, snippet) => globalThis.__cowE2E.remoteEval(snippet), code)

/** The container's state, read from the main process. */
const containerInfo = () =>
  app.evaluate(() => globalThis.__cowE2E.remoteContainerInfo())

/** The next authorization URL the app opened, which no earlier attempt used. */
async function nextAuthorizeUrl() {
  const found = await waitFor('the authorize URL', () => {
    const urls = openedExternal()
    const candidate = urls.find((url) => url.includes('/auth/desktop/authorize')
      && !authorizeUrlSeen.has(url))
    return candidate || null
  }, { timeout: 60000 })
  authorizeUrlSeen.add(found)
  return found
}

/**
 * Click a button in the shell.
 *
 * The container view covers the whole window (``container.ts``: ``TOP_INSET`` 0),
 * so a *pointer* event aimed at the shell can be swallowed by the native view on
 * top of it. The click is therefore delivered to the shell document itself when
 * Playwright's own click does not land -- the same button, the same React
 * handler, no product code skipped.
 */
async function clickInShell(name) {
  const button = shell.getByRole('button', { name })
  try {
    await button.click({ timeout: 10000 })
  } catch {
    await shell.evaluate((pattern) => {
      const match = [...document.querySelectorAll('button')]
        .find((node) => new RegExp(pattern, 'i').test(node.innerText || ''))
      if (!match) throw new Error(`no button matching ${pattern}`)
      match.click()
    }, name.source)
  }
}

/**
 * Wait until the broker can actually serve a sign-in.
 *
 * The sign-in gate is shown as soon as the *renderer* concludes the backend is
 * up -- and after a failed probe it is shown deliberately, because "a failed
 * probe is never no-login-required" (``context.ts``). The main process learns
 * the backend's ephemeral origin separately, on the manager's ``ready`` event,
 * which can land a moment later. A human who clicks in that window is told
 * "backend is not ready" and offered a retry; this waits for the same condition
 * the button really needs, so the spec does not race the product's own startup.
 */
async function waitForBrokerReady() {
  await waitFor('the identity broker to reach the backend', () =>
    shell.evaluate(() => window.electronAPI.desktopAuthProbe()
      .then((reply) => !!reply && reply.ok === true)
      .catch(() => false)))
}

/** Start a browser authorization and perform its browser leg. */
async function authorize(name, credentials) {
  await waitForBrokerReady()
  await clickInShell(name)
  const url = await nextAuthorizeUrl()
  const backendOrigin = new URL(url).origin
  await completeBrowserLeg(url, { ...credentials, origin: backendOrigin })
  return backendOrigin
}

/** Sign in to the bundled backend (local mode). */
async function signInLocally() {
  const backendOrigin = await authorize(LABEL.login, LOCAL_USER)
  assert.match(backendOrigin, /^http:\/\/127\.0\.0\.1:\d+$/,
    `the bundled backend is a plain loopback origin, got ${backendOrigin}`)
  // The gate is gone only once the session is accepted and the projection is in.
  await waitFor('the local session to be accepted', () =>
    shell.evaluate(() => !/在浏览器中登录|Sign in with browser/.test(document.body.innerText)))
  // A first sign-in on a fresh profile opens the onboarding wizard. Dismiss it
  // the way the wizard itself offers -- "skip" -- so the shell underneath is
  // reachable; it is a real user action on a real button.
  const skip = shell.getByRole('button', { name: /^(跳过|Skip)$/ })
  try {
    await skip.first().click({ timeout: 20000 })
  } catch {
    /* no wizard on this profile */
  }
  await waitFor('the onboarding wizard to be gone', () =>
    shell.evaluate(() => !/第 \d+ \/ \d+ 步|Step \d+ of \d+/.test(document.body.innerText)))
  return backendOrigin
}

/**
 * Bring the container's console to the workbench, the way the user does.
 *
 * The page renders its markup first and resolves the account afterwards, so
 * ``#chat-input`` exists before the console has decided what to show. An account
 * that belongs to more than one tenant has no tenant stored in a fresh tab, and
 * the console answers that state with its own picker (``_showTenantPicker``:
 * ``#login-tenant-group`` + ``#login-tenant-select`` + ``#login-btn``); driving
 * the page before that choice is made sends the turn into the overlay. The
 * selection is made on the picker's own control -- the same select, the same
 * button, the same ``change``/click handler a user triggers.
 */
async function openWorkbenchInContainer() {
  await waitFor('the console to render in the container', () =>
    remote('!!document.querySelector("#chat-input")'), { timeout: 60000 })

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
          && overlay && overlay.classList.contains('hidden')) {
        return { picker: null };
      }
      return null;
    })()`), { timeout: 60000 })

  if (settled.picker) {
    assert.ok(settled.picker.includes(FIRST_TENANT),
      `the picker must offer the tenant this journey uses: ${JSON.stringify(settled.picker)}`)
    await remote(`(() => {
      const select = document.getElementById('login-tenant-select');
      select.value = ${JSON.stringify(FIRST_TENANT)};
      document.getElementById('login-btn').click();
      return true;
    })()`)
  }

  await waitFor('the workbench to open in the container', () =>
    remote(`(() => {
      const app = document.getElementById('app');
      const overlay = document.getElementById('login-overlay');
      return !!(app && !app.classList.contains('hidden')
        && overlay && overlay.classList.contains('hidden'));
    })()`), { timeout: 60000 })
}

/** Enter remote mode and attach the server's console. */
async function attachRemote(as = USERS.u1) {
  const backendOrigin = await authorize(LABEL.enterRemote, as)
  assert.equal(backendOrigin, ORIGIN, 'the authorization must go to the active server')
  await waitFor('the container to attach', async () => (await containerInfo()).attached, { timeout: 60000 })
  await openWorkbenchInContainer()
}

const shellProfiles = async () =>
  (await shell.evaluate(() => window.electronAPI.desktopModeGet()))?.profiles || []

// --------------------------------------------------------------------------
// 1. local mode: sign-in, the entry point, the probe and the switch
// --------------------------------------------------------------------------

test('a fresh profile boots local, runs the bundled backend and signs in through the browser', async () => {
  await launch({ remote: false })

  // The default: an install that has never been configured is local, and the
  // local backend is the one that was started.
  assert.ok(!launchedLog().includes('[remote] remote mode'),
    `a fresh install must boot in local mode; log: ${launchedLog().slice(0, 400)}`)
  const mode = await shell.evaluate(() => window.electronAPI.desktopModeGet())
  assert.equal(mode.mode, 'local')
  assert.equal(mode.containerSupported, true, 'this runtime must support the container')

  const backendOrigin = await signInLocally()
  assert.ok(backendOrigin.startsWith('http://127.0.0.1:'),
    'the bundled backend is announced as a loopback origin')
})

test('the local shell reaches the connection surface from Settings', async () => {
  // The app uses a HashRouter, so a route is an address, not a history call.
  await shell.evaluate(() => { window.location.hash = '#/settings' })
  const open = shell.getByRole('button', { name: LABEL.openShell })
  await open.waitFor({ timeout: 30000 })
  await open.click()
  await waitFor('the connection shell', () =>
    shell.evaluate(() => window.location.hash === '#/remote'))
  await shell.locator('input[placeholder^="https://"]').waitFor({ timeout: 15000 })
})

test('a plain HTTP address is refused and never stored', async () => {
  const origin = shell.locator('input[placeholder^="https://"]')
  await origin.fill('http://127.0.0.1:8080')
  await clickInShell(LABEL.checkAndAdd)
  const refusal = shell.getByText(/仅支持根路径部署的精确 HTTPS|Only an exact HTTPS origin/)
  await refusal.waitFor({ timeout: 20000 })
  assert.deepEqual(await shellProfiles(), [], 'an HTTP address must not be stored')
})

test('the exact HTTPS origin is probed and stored, then made the active server', async () => {
  const origin = shell.locator('input[placeholder^="https://"]')
  await origin.fill(ORIGIN)
  await clickInShell(LABEL.checkAndAdd)
  await shell.getByText(ORIGIN, { exact: true }).waitFor({ timeout: 30000 })

  const profiles = await shellProfiles()
  assert.equal(profiles.length, 1, 'exactly one server must have been stored')
  assert.equal(profiles[0].origin, ORIGIN)

  // Selecting it is what makes it the one active server -- and the only state in
  // which "enter remote mode" is offered.
  await shell.getByText(ORIGIN, { exact: true }).click()
  const mode = await waitFor('the server to become active', async () => {
    const projection = await shell.evaluate(() => window.electronAPI.desktopModeGet())
    return projection?.activeProfileId ? projection : null
  })
  assert.equal(mode.activeProfileId, profiles[0].id)

  // A second HTTPS origin that does not answer is refused by the probe and is
  // never stored.
  await origin.fill('https://127.0.0.1:1')
  await clickInShell(LABEL.checkAndAdd)
  await shell.getByText(/无法连接到服务器|could not be reached/).first().waitFor({ timeout: 30000 })
  assert.equal((await shellProfiles()).length, 1, 'a server that did not answer must not be stored')
})

test('entering remote mode authorizes this client and attaches the server console', async () => {
  await attachRemote()

  const mode = await shell.evaluate(() => window.electronAPI.desktopModeGet())
  assert.equal(mode.mode, 'remote')
  // The stored config names the one active server, and nothing else.
  assert.equal(mode.activeProfileId, mode.profiles[0].id)
})

// --------------------------------------------------------------------------
// 2. the container and the native boundary
// --------------------------------------------------------------------------

test('the container runs the server console with the narrow bridge and nothing wider', async () => {
  const info = await containerInfo()
  assert.ok(info.attached, 'the container must be attached')
  // It is a ``WebContentsView`` on a partition of its own, not a window and not
  // the shell's session: the child cookie lives in memory and dies with the
  // process (``web-session.ts``: ``partitionName`` has no ``persist:`` prefix).
  assert.equal(info.persistent, false, 'the child partition must not persist')

  const page = await remote(`(() => ({
    origin: location.origin,
    path: location.pathname,
    hasElectronAPI: typeof window.electronAPI !== 'undefined',
    hasDesktopHost: typeof window.desktopHost === 'object',
    hasCowHost: typeof window.CowDesktopHost === 'object',
  }))()`)
  assert.equal(page.origin, ORIGIN)
  assert.ok(page.path.startsWith('/'), `unexpected path ${page.path}`)
  assert.equal(page.hasElectronAPI, false, 'the remote page must not get the local shell bridge')
  assert.equal(page.hasDesktopHost, true, 'the remote page must get the narrow bridge')
  assert.equal(page.hasCowHost, true, 'the console adapter must be loaded')

  const capabilities = await remote('window.CowDesktopHost.capabilities()')
  assert.equal(capabilities.available, true)
  assert.equal(capabilities.environment, 'desktop')
  assert.equal(String(capabilities.bridge).split('.')[0], '1')
  assert.deepEqual(capabilities.methods.sort(), [
    'getCapabilities', 'onHostEvent', 'openExternal', 'saveArtifact', 'suspendLocalContext',
  ])

  // Phase 1 creates no local file grant, and the adapter exposes no path API.
  const files = await remote('window.CowDesktopHost.localFiles()')
  assert.deepEqual(files, { available: false, reason: 'phase_two' })
})

test('a page cannot forge the bridge or reach a native capability it was not given', async () => {
  const forged = await remote(`(() => {
    const before = window.CowDesktopHost.isDesktop();
    const real = window.desktopHost;
    try {
      window.desktopHost = { getCapabilities: () => ({ bridge: '1.0' }) };
    } catch (_) { /* a frozen global is also a refusal */ }
    const after = window.CowDesktopHost.isDesktop();
    window.desktopHost = real;
    return { before, after };
  })()`)
  assert.equal(forged.before, true)
  assert.equal(forged.after, true, 'a page-supplied host object must not be trusted')

  // The bridge exposes no generic invoke: a page cannot reach a native
  // capability it was not given, and there is no way to ask for an arbitrary
  // method by name.
  const unknown = await remote('typeof window.desktopHost.invoke')
  assert.equal(unknown, 'undefined', 'the bridge must expose no generic invoke')

  // The bridge is the *only* native surface, and it is not the local preload's.
  const surfaces = await remote(`Object.keys(window.desktopHost).sort()`)
  assert.deepEqual(surfaces, ['getCapabilities', 'onHostEvent', 'openExternal', 'saveArtifact', 'suspendLocalContext'])
})

// --------------------------------------------------------------------------
// 3. the business pages carried in the container
// --------------------------------------------------------------------------

test('a chat turn streams token by token from the real upstream', async () => {
  const before = (await control('/model-requests')).count
  await remote(`(() => {
    const input = document.querySelector('#chat-input');
    input.value = 'ping';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    window.sendMessage();
    return true;
  })()`)

  // The tokens arrive with a visible gap; the first one must be on screen
  // before the last one exists, which a single buffered blob could not show.
  const text = () =>
    remote(`(() => {
      const nodes = document.querySelectorAll('#chat-messages, #messages, main');
      return nodes.length ? nodes[nodes.length - 1].innerText : document.body.innerText;
    })()`)
  await waitFor('the first streamed token', async () => (await text()).includes('E2E-'), { timeout: 90000 })
  const partial = await text()
  assert.ok(!partial.includes('E2E-stream-ok'), 'the whole answer arrived at once; it did not stream')
  await waitFor('the finished answer', async () => (await text()).includes('E2E-stream-ok'), { timeout: 90000 })

  const after = await waitFor('the upstream to be called', async () => {
    const count = (await control('/model-requests')).count
    return count > before ? count : null
  }, { timeout: 30000 })
  assert.ok(after > before, 'the canned upstream was never asked')
})

test('switching tenant is a one-shot address that replaces the previous document context', async () => {
  // The premise of "a late answer from the old tenant is discarded": the host
  // stamps every document with a monotonic generation and refuses a call that
  // names an older one (``container.ts``: bumped on every committed navigation;
  // ``host-bridge.ts``: ``stale_context``). Reading it before and after the
  // switch shows the document really was replaced, not reused.
  const generationBefore = await remote(
    'window.desktopHost.getCapabilities().then((reply) => reply.generation)')
  assert.equal(typeof generationBefore, 'number')

  const target = `${ORIGIN}/chat?switch_tenant=${SECOND_TENANT.tenant_id}`
  await remote(`(location.assign(${JSON.stringify(target)}), true)`)
  await waitFor('the tenant switch to commit', () =>
    remote(`sessionStorage.getItem('cow_tenant_id') === ${JSON.stringify(SECOND_TENANT.tenant_id)}`),
    { timeout: 60000 })

  const state = await remote(`(() => ({
    tenant: sessionStorage.getItem('cow_tenant_id'),
    url: location.href,
  }))()`)
  assert.equal(state.tenant, SECOND_TENANT.tenant_id)
  assert.ok(!state.url.includes('switch_tenant'), 'the one-shot parameter must be stripped')

  const generationAfter = await remote(
    'window.desktopHost.getCapabilities().then((reply) => reply.generation)')
  assert.ok(generationAfter > generationBefore,
    `the switch must be a new document generation (${generationBefore} -> ${generationAfter})`)

  // The server really serves this tenant for the child session -- the same
  // cookie, a different X-Tenant-ID selection.
  const second = await remote(`fetch('/auth/context', {
    credentials: 'same-origin',
    headers: { 'X-Tenant-ID': ${JSON.stringify(SECOND_TENANT.tenant_id)} },
  }).then((r) => r.json())`)
  assert.equal(second.status, 'success', `the child session must be authorized for the second tenant: ${JSON.stringify(second)}`)

  // And back, so the switch is not one-way.
  await remote(`(location.assign(${JSON.stringify(`${ORIGIN}/chat?switch_tenant=${FIRST_TENANT}`)}), true)`)
  await waitFor('the switch back', () =>
    remote(`sessionStorage.getItem('cow_tenant_id') === ${JSON.stringify(FIRST_TENANT)}`), { timeout: 60000 })
  const first = await remote(`fetch('/auth/context', {
    credentials: 'same-origin',
    headers: { 'X-Tenant-ID': ${JSON.stringify(FIRST_TENANT)} },
  }).then((r) => r.json())`)
  assert.equal(first.status, 'success')
  assert.ok(await remote(
    'window.desktopHost.getCapabilities().then((reply) => reply.generation)') > generationAfter,
  'switching back is another committed navigation')
})

test('a same-origin upload through the page file input reaches the server', async () => {
  // The console reads a real File out of the composer's own attachment input
  // (``#file-input``); the bytes go to the real upload endpoint with the child
  // cookie. The page carries other file inputs (the account avatar, the
  // knowledge importer), so the composer's must be addressed by id.
  const dispatched = await remote(`(() => {
    const input = document.querySelector('#file-input');
    if (!input) return 'no-input';
    const file = new File(['e2e-upload-body'], 'e2e-中文名.txt', { type: 'text/plain' });
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return 'dispatched';
  })()`)
  assert.equal(dispatched, 'dispatched', 'the console must offer a file input (W19)')
  const uploaded = await waitFor('the upload to be stored', async () => {
    const res = await control(`/workspace/uploaded?name=${encodeURIComponent('e2e-中文名.txt')}`)
    return res.content || null
  }, { timeout: 60000 })
  assert.equal(uploaded, 'e2e-upload-body')
})

test('W20: a foreign link leaves for the system browser and never replaces the shell', async () => {
  const before = openedExternal().length
  const urlBefore = (await containerInfo()).url
  await remote(`(window.open('https://example.com/e2e-external'), true)`)
  const url = await waitFor('the external URL to be handed to the system browser', () => {
    const urls = openedExternal()
    return urls.length > before ? urls[urls.length - 1] : null
  })
  assert.ok(url.startsWith('https://example.com/e2e-external'))
  // A popup was answered in-page (so the caller does not hang) and the shell
  // document is untouched.
  assert.equal((await containerInfo()).url, urlBefore)
})

test('W20: an in-page navigation off the console origin is refused, not loaded', async () => {
  const before = openedExternal().length
  const urlBefore = (await containerInfo()).url
  await remote(`(location.assign('https://example.com/e2e-navigate'), true)`)
  const url = await waitFor('the foreign navigation to be handed to the system browser', () => {
    const urls = openedExternal()
    return urls.length > before ? urls[urls.length - 1] : null
  })
  assert.equal(url, 'https://example.com/e2e-navigate')
  await sleep(1000)
  assert.equal((await containerInfo()).url, urlBefore,
    'the container must still be on the console, not on the foreign origin')
  assert.ok((await containerInfo()).url.startsWith(ORIGIN))
})

test('W20: the container denies media access to the remote page', async () => {
  const verdict = await remote(`navigator.mediaDevices.getUserMedia({ audio: true })
    .then(() => 'granted')
    .catch((error) => String((error && error.name) || 'error'))`)
  assert.notEqual(verdict, 'granted', 'a remote page must not be granted a media device')
  assert.ok(['NotAllowedError', 'SecurityError', 'NotFoundError'].includes(verdict),
    `expected a refusal, got ${verdict}`)
})

test('revoking the child session server-side stops business requests, not just a probe', async () => {
  const revoked = await control('/child/revoke')
  assert.equal(revoked.revoked, true, 'no live child link to revoke')
  const answer = await remote(`fetch('/api/sessions', {
    credentials: 'same-origin',
    headers: { 'X-Tenant-ID': ${JSON.stringify(FIRST_TENANT)} },
  }).then((r) => ({ status: r.status })).catch(() => ({ status: 0 }))`)
  assert.ok(answer.status === 401 || answer.status === 403,
    `a revoked child must not be served business data (got ${answer.status})`)
})

// --------------------------------------------------------------------------
// 4. relaunch, detach and the way back to local
// --------------------------------------------------------------------------

test('the next launch boots remote without the local backend and requires a fresh authorization', async () => {
  await restart({ remote: true })
  assert.ok(launchedLog().includes('[remote] remote mode: local backend not started'))
  assert.ok(!launchedLog().includes('Backend ready on port'),
    'remote mode must not start the bundled business backend')
  assert.equal((await containerInfo()).attached, false, 'no container may survive a restart')

  // The server is remembered; the session is not.
  const profiles = await shellProfiles()
  assert.equal(profiles.length, 1)
  assert.equal(profiles[0].origin, ORIGIN)

  await attachRemote()
  assert.ok((await containerInfo()).attached, 're-authorization must attach the container again')
})

test('detaching ends the pairing on both sides', async () => {
  const linksBefore = await control('/links')
  const current = linksBefore.links.filter((link) => !link.revoked).pop()
  assert.ok(current, 'the attach must have created a live link')

  await clickInShell(LABEL.disconnect)
  await waitFor('the container to detach', async () => !(await containerInfo()).attached)

  // Detaching is "block first, revoke second" (remote-container-ipc.ts): the
  // view and the bridge go before the sign-out request, so ``attached`` is
  // already false while the server-side revoke is still in flight. The link is
  // therefore polled rather than read once -- what must be true is that the
  // pairing does not *survive* the detach.
  const links = await waitFor('the server to revoke the pairing', async () => {
    const state = await control('/links')
    const row = state.links.find((link) => link.id === current.id)
    return row && row.revoked ? state : null
  }, { timeout: 30000 })
  const revoked = links.links.find((link) => link.id === current.id)
  assert.ok(revoked && revoked.revoked, 'the server must not keep this pairing live after a detach')
  const session = await control(`/child/session?id=${encodeURIComponent(current.id)}`)
  assert.equal(session.revoked, true, 'the linked Web child session must be revoked in the store')
})

test('switching back to local restores the bundled backend on the next launch', async () => {
  await clickInShell(LABEL.backLocal)
  await restart({ remote: false })
  assert.ok(!launchedLog().includes('[remote] remote mode'),
    'the app must boot local again after switching back')
  // The remote session did not survive the switch: the local shell asks for a
  // sign-in, which is the app's own way of saying no session is held.
  await shell.getByRole('button', { name: LABEL.login }).waitFor({ timeout: 90000 })
})

after(async () => {
  if (app) await app.close().catch(() => undefined)
})
