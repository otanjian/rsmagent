// Test-only Electron main entry for the remote-workbench E2E (task 5.8).
//
// This file is *not* shipped (electron-builder packages ``dist/**`` and
// ``resources/**`` only). It exists because three things a real launch does
// cannot happen in an automated test, and each of them is replaced here rather
// than in the product:
//
//  1. **A private profile.** The app's userData, downloads and ``~/.cow`` are
//     redirected into the run's own directory, so a test never reads or writes
//     the operator's real config, window state or download folder. The spec
//     spawns Electron with ``HOME`` pointed at the same directory, so nothing
//     escapes it. ``appData`` is redirected too: the app derives its profile
//     directory from *that* root (``index.ts``: ``appData/<profileName>``), so
//     redirecting only ``userData`` would be undone a moment later and the run
//     would land in the operator's real application-support folder.
//
//  2. **The server's test certificate.** The fixture serves real HTTPS with a
//     self-signed certificate. Two trust decisions are needed, both scoped to
//     this process and neither of them a product option:
//       * ``NODE_EXTRA_CA_CERTS`` (set by the runner) makes Node's TLS -- what
//         the native broker's ``fetch`` uses -- verify the chain against the
//         test CA. Verification still happens; only the CA is added.
//       * ``--ignore-certificate-errors-spki-list`` pins the *public key* of
//         that one certificate for Chromium, which owns the container's page
//         load and does not read ``NODE_EXTRA_CA_CERTS``. A pin is narrower
//         than "ignore certificate errors": every other certificate is still
//         refused.
//
//  3. **The browser leg of the native sign-in.** ``auth-broker`` hands the
//     authorization URL to ``shell.openExternal``, which would open the
//     operator's real browser. Here that single call is recorded to a file so
//     the spec can perform the same GET+consent POST a browser would. The
//     broker itself is untouched: it still generates the PKCE verifier, opens
//     the loopback listener, waits for the code and exchanges it.
//
//  4. **The native directory dialog.** See the block below: a native sheet
//     cannot be clicked by a test, so its two OS-owned decisions (whether a
//     result comes back, and how long the user takes) are scripted. The
//     product's own selection handler, service and grant registry still run.
//
// Everything else -- mode selection, the config file, probing, the container,
// the narrow bridge, downloads -- is the product's own code.

'use strict'

const { app, BaseWindow, shell, webContents } = require('electron')
const fs = require('fs')
const path = require('path')

const profileDir = process.env.COW_E2E_PROFILE_DIR
if (!profileDir) {
  console.error('[e2e] COW_E2E_PROFILE_DIR is required (the runner sets it)')
  app.exit(2)
}

for (const name of ['userData', 'downloads', 'cow', 'appData']) {
  fs.mkdirSync(path.join(profileDir, name), { recursive: true })
}
// Before ``app.whenReady``: the config file, window state and Chromium profile
// all derive from these roots, so this has to be settled first. ``appData`` is
// the OS-level root the app composes its own profile directory from (see the
// header); ``userData`` and ``downloads`` are set as well so anything that asks
// for them directly -- before or after the app's own naming runs -- is inside
// the sandbox too.
app.setPath('appData', path.join(profileDir, 'appData'))
app.setPath('userData', path.join(profileDir, 'userData'))
app.setPath('downloads', path.join(profileDir, 'downloads'))

const spki = process.env.COW_E2E_SPKI || ''
if (spki) {
  app.commandLine.appendSwitch('ignore-certificate-errors-spki-list', spki)
}

// Every external URL the app asks the system to open. The spec reads this file;
// nothing is launched.
const openedPath = path.join(profileDir, 'opened-external.jsonl')
shell.openExternal = async (url) => {
  fs.appendFileSync(openedPath, `${JSON.stringify({ url: String(url), at: Date.now() })}\n`)
  return ''
}

// ---------------------------------------------------------------------------
// The native directory dialog, scripted and recorded.
//
// A real ``showOpenDialog`` is a native sheet: nothing in a test can click the
// filesystem browser it puts on screen, and it blocks the window it is attached
// to. The two decisions the OS makes on the user's behalf -- *whether* a result
// comes back at all, and *how long* the user takes to make it -- are therefore
// scripted here. Everything downstream is the product's own code: the same
// ``chooseWorkspace`` handler, the same selection service, the same grant
// registry, the same console entry point. What is substituted is only the part
// that is not ours.
//
// ``dialog-script.json`` is a JSON array read fresh on every call:
//   { "result": { "filePaths": ["/tmp/x"] } }   what the user picked
//   { "result": { "canceled": true } }          the user closed the dialog
//   { "hold": "a" }                             wait until "a" is released
//   { "delayMs": 300 }                          answer after a pause
// The missing step is a cancel, so a script that runs out of steps closes the
// remaining dialogs rather than leaving them unanswered forever.
//
// Releases are polled from ``dialog-releases.json`` (an array of hold ids) and
// calls are appended to ``dialog-calls.jsonl`` -- one ``call`` record when the
// product asks, one ``return`` record when this override answers -- so the spec
// can prove the order the app asked and the order it was answered in, which is
// what the stale-callback cases turn on.
const dialogScriptPath = path.join(profileDir, 'dialog-script.json')
const dialogReleasesPath = path.join(profileDir, 'dialog-releases.json')
const dialogCallsPath = path.join(profileDir, 'dialog-calls.jsonl')
const dialog = require('electron').dialog
let dialogIndex = 0

function readJsonFile(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return fallback
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

dialog.showOpenDialog = async (...args) => {
  // The product calls this two ways: ``(parent, options)`` in the bridge
  // handler and ``(options)`` from the local ``select-directory`` handler.
  const options = (args.length > 1 ? args[1] : args[0]) || {}
  // The script file *is* the queue: each step is consumed by being removed, so a
  // spec that writes a fresh array starts a fresh sequence. A counter would have
  // to be reset alongside the file and the two could disagree.
  const steps = readJsonFile(dialogScriptPath, [])
  const step = steps.shift() || { result: { canceled: true } }
  fs.writeFileSync(dialogScriptPath, JSON.stringify(steps))
  const { index } = { index: ++dialogIndex }
  fs.appendFileSync(
    dialogCallsPath,
    `${JSON.stringify({
      kind: 'call',
      index,
      message: options.message || '',
      properties: options.properties || [],
      at: Date.now(),
    })}\n`,
  )
  if (step.hold) {
    while (!readJsonFile(dialogReleasesPath, []).includes(step.hold)) await sleep(20)
  }
  if (step.delayMs) await sleep(step.delayMs)
  const result = step.result || { canceled: true }
  fs.appendFileSync(
    dialogCallsPath,
    `${JSON.stringify({ kind: 'return', index, canceled: !!result.canceled, at: Date.now() })}\n`,
  )
  return { canceled: !!result.canceled, filePaths: result.filePaths || [] }
}

// One test-only hook, for reading the state Playwright cannot see: a
// ``WebContentsView`` is not a ``BrowserWindow``, so ``electronApp.windows()``
// never returns the container's page. It is reachable through the main process
// instead, and this is that accessor -- it exposes no capability the page does
// not already have from the app itself.

/** Every view in a window's view tree, the given one included. */
function viewsIn(view) {
  const children = Array.isArray(view.children) ? view.children : []
  return [view, ...children.flatMap(viewsIn)]
}

/** A view that owns a page (``WebContentsView``), or null. */
function contentsOfView(view) {
  const contents = view.webContents
  if (contents && typeof contents.getURL === 'function' && typeof contents.executeJavaScript === 'function') {
    return contents
  }
  return null
}

globalThis.__cowE2E = {
  /**
   * The container's WebContents, or null when nothing is attached.
   *
   * The container is a ``WebContentsView`` attached to a window's view tree,
   * and that is exactly how it is found: walk every window's ``contentView``
   * (``View.children`` is the documented accessor) and take the view that owns
   * a page. A content window is a window of its own, so it is never a view of
   * another window and can never be mistaken for the container.
   *
   * An earlier version of this accessor called ``Session.getPartition()``,
   * which does not exist on this Electron line and whose ``startsWith`` check
   * threw on every candidate: the lookup then answered "nothing attached" for a
   * container that was loaded and running, and every test that needed the page
   * failed on the accessor rather than on the app.
   */
  remoteWebContents() {
    for (const window of BaseWindow.getAllWindows()) {
      for (const view of viewsIn(window.contentView)) {
        const contents = contentsOfView(view)
        if (contents && !contents.isDestroyed()) return contents
      }
    }
    return null
  },
  /** The container's webContents id, or null when nothing is attached. */
  remoteWebContentsId() {
    const found = globalThis.__cowE2E.remoteWebContents()
    return found ? found.id : null
  },
  /**
   * What the container *is*, read from the main process.
   *
   * ``persistent`` is the property the E2E asserts on -- the child cookie's
   * partition is in memory and dies with the process (``web-session.ts``:
   * ``partitionName`` carries no ``persist:`` prefix) -- reported as the runtime
   * computes it rather than as a partition name the test would have to guess.
   */
  remoteContainerInfo() {
    const found = globalThis.__cowE2E.remoteWebContents()
    if (!found) return { attached: false }
    return {
      attached: true,
      id: found.id,
      url: found.getURL(),
      persistent: found.session.isPersistent(),
    }
  },
  /** Run a snippet inside the container's page. Refuses when none is attached. */
  remoteEval(code) {
    const id = globalThis.__cowE2E.remoteWebContentsId()
    if (id === null) return Promise.reject(new Error('no remote container is attached'))
    const target = webContents.fromId(id)
    if (!target) return Promise.reject(new Error('the remote container is gone'))
    return target.executeJavaScript(code, true)
  },
  /**
   * The live local-directory grants, read from the authority.
   *
   * The grant registry is the thing that decides whether a local root may be
   * read, and a page is deliberately unable to enumerate it. Reading it here --
   * from the main process, through the same module instance the app loaded --
   * is what lets the spec state "cancelling created no authorization" as a fact
   * about the authorization table rather than an inference from the UI. Paths
   * are returned separately, keyed by grant id, because the public projection
   * does not carry them (that is a property the release relies on).
   */
  grantRegistry() {
    const bridge = require(path.join(__dirname, '..', 'dist', 'main', 'remote', 'local-files-bridge.js'))
    return bridge.remoteGrantRegistry.list().map((grant) => ({
      id: grant.id,
      label: grant.label,
      purpose: grant.purpose,
      grantVersion: grant.grantVersion,
      tenantId: grant.tenantId,
      deviceId: grant.deviceId,
      absolutePath: bridge.remoteGrantRegistry.absolutePathFor(grant.id),
    }))
  },
  /**
   * Where the installation identity lives, and what it currently holds.
   *
   * Asked of the app rather than hard-coded: ``index.ts`` composes its profile
   * directory as ``appData/<profileName>``, so the path a test would guess
   * (``userData``) is not the one the app uses -- guessing it made an "identity
   * missing" assertion pass for the wrong reason.
   */
  installationIdentity() {
    const mod = require(path.join(__dirname, '..', 'dist', 'main', 'installation-identity.js'))
    const file = mod.installationIdentityPath(require('electron').app.getPath('userData'))
    let raw = null
    try {
      raw = fs.readFileSync(file, 'utf8')
    } catch {
      raw = null
    }
    return { file, raw }
  },
  /**
   * Clear the identity the way an uninstall or a support reset would: the file
   * goes and the memoised value goes with it, so the next read is a real first run.
   */
  resetInstallationIdentity() {
    const mod = require(path.join(__dirname, '..', 'dist', 'main', 'installation-identity.js'))
    const file = mod.installationIdentityPath(require('electron').app.getPath('userData'))
    mod.forgetInstallationIdentity()
    fs.rmSync(file, { force: true })
    return file
  },
}

require(path.join(__dirname, '..', 'dist', 'main', 'index.js'))
