// The isolation probe for a local preview (change
// align-desktop-project-execution-with-master, task 9.5, acceptance A25).
//
// Run:  desktop/node_modules/.bin/electron desktop/e2e/preview-isolation.probe.cjs
//
// The unit suite (`tests/test_desktop_isolated_preview.cjs`) proves the decision,
// the ticket and the policy *as values*. It cannot prove what Chromium does with
// them, and four of this task's claims are exactly that:
//
//   1. a `cow-preview:` URL is embeddable from a real page at all (the scheme
//      needs the right privileges, and a scheme that is refused as mixed content
//      would make every local preview a blank frame);
//   2. a previewed HTML document runs under the response's CSP -- opaque origin,
//      no cookie, no storage -- so it cannot reach the console's session;
//   3. it has no bridge: no `desktopHost`, no `electronAPI`, no `require`, and
//      no way to reach its parent's document;
//   4. it cannot initiate network I/O, and the page cannot read the file's bytes
//      with `fetch` -- measured from *both* ends, including the server's own
//      request log;
//   5. the URL stops working when the ticket is revoked.
//
// So this probe builds the smallest thing that can be measured: a real HTTP page
// (with a real cookie and a sentinel bridge), one preview frame, one preview
// image, and a real `protocol.handle` on a real session. Nothing here is a mock;
// the only simplification is that the page is served by this file's own server
// instead of by the console.
//
// The probe prints one JSON line per check and exits non-zero if any of them
// failed. Its output is kept as evidence (see `evidence/isolated-preview.md`).

const { app, BrowserWindow, protocol, session } = require('electron');
const http = require('node:http');
const path = require('node:path');

const DIST = path.join(__dirname, '..', 'dist', 'main');
// The *shipping* module, not a copy: the policy, the ticket store and the answer
// mapping measured here are the ones the application runs.
const preview = require(path.join(DIST, 'project-browser', 'preview.js'));

// Declared exactly the way `remote-container-ipc.ts` declares them, from the same
// exported value -- a probe that registered its own privileges would be
// measuring a scheme the product does not ship.
protocol.registerSchemesAsPrivileged([
  { scheme: preview.PREVIEW_SCHEME, privileges: preview.PREVIEW_SCHEME_PRIVILEGES },
]);

const results = [];
function check(name, ok, detail) {
  results.push({ check: name, ok: !!ok, detail: detail === undefined ? '' : detail });
  console.log(JSON.stringify({ probe: name, ok: !!ok, detail: detail === undefined ? '' : detail }));
}

/** The previewed document: markup of its own, and every question A25 asks. */
const PREVIEW_HTML = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>preview</title>
<style>body { background: rgb(1, 2, 3); }</style></head>
<body>
<div id="styled">generated report</div>
<script>
(function () {
  var facts = {};
  facts.scriptRan = true;
  facts.origin = String(location.origin);
  facts.href = String(location.href);
  try { facts.cookie = String(document.cookie); } catch (e) { facts.cookie = 'threw'; }
  try { window.localStorage.setItem('k', 'v'); facts.storage = 'readable'; }
  catch (e) { facts.storage = 'blocked'; }
  try { void parent.document.body; facts.parentDom = 'readable'; }
  catch (e) { facts.parentDom = 'blocked'; }
  try { void top.location.href; facts.topLocation = 'readable'; }
  catch (e) { facts.topLocation = 'blocked'; }
  facts.hasDesktopHost = typeof window.desktopHost !== 'undefined';
  facts.hasElectronApi = typeof window.electronAPI !== 'undefined';
  facts.hasRequire = typeof window.require !== 'undefined';
  facts.hasProcess = typeof window.process !== 'undefined';
  facts.hasModule = typeof window.module !== 'undefined';
  facts.hasIpcRenderer = typeof window.ipcRenderer !== 'undefined';
  facts.style = window.getComputedStyle(document.body).backgroundColor;
  window.__facts = facts;
  // Report asynchronously, so the parent has something to wait for and the
  // script's own existence is observable from outside the frame.
  parent.postMessage({ probe: 'preview-frame', facts: facts }, '*');
})();
</script>
</body></html>`;

/** The page that embeds the preview: the console, with a sentinel bridge. */
const CONSOLE_HTML = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>console</title></head>
<body>
<script>
window.desktopHost = { call: function () { return 'the bridge'; } };
window.electronAPI = { invoke: function () { return 'ipc'; } };
window.__messages = [];
window.addEventListener('message', function (event) {
  window.__messages.push({ origin: String(event.origin), data: event.data });
});
</script>
</body></html>`;

const server = { hits: [], requests: [] };

function startServer() {
  return new Promise((resolve) => {
    const instance = http.createServer((req, res) => {
      server.requests.push(req.url);
      if (req.url === '/secret') {
        server.hits.push(req.url);
        res.writeHead(200, { 'Content-Type': 'text/plain' });
        res.end('the secret the preview must not reach');
        return;
      }
      if (req.url === '/ws') {
        server.hits.push(req.url);
        // An upgrade that must never be attempted: `connect-src 'none'` covers
        // WebSocket, and the proof is that this branch is never entered.
        res.writeHead(400);
        res.end('no');
        return;
      }
      if (req.url === '/console') {
        // A cookie worth stealing, on the origin that holds the bridge.
        res.writeHead(200, {
          'Content-Type': 'text/html; charset=utf-8',
          'Set-Cookie': 'console_session=super-secret; Path=/',
        });
        res.end(CONSOLE_HTML);
        return;
      }
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('no');
    });
    instance.listen(0, '127.0.0.1', () => resolve(instance));
  });
}

function url(instance) {
  return `http://127.0.0.1:${instance.address().port}`;
}

/** Run one expression in the *page*, and get its value back. */
async function inPage(win, expression) {
  return win.webContents.executeJavaScript(expression, true);
}

/** The one frame whose URL is a preview, or `undefined`. */
function previewFrame(win) {
  return win.webContents.mainFrame.frames.find((frame) => frame.url.startsWith(`${preview.PREVIEW_SCHEME}:`));
}

async function waitFor(fn, { timeout = 8000, interval = 50 } = {}) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const value = await fn();
    if (value) return value;
    if (Date.now() > deadline) return null;
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
}

async function main() {
  const instance = await startServer();
  const origin = url(instance);

  const store = preview.createPreviewTickets({ ttlMs: 60000 });
  // The page and its preview share one session, exactly as the container and its
  // preview do: the scheme handler is per session, so a page in another session
  // would be a page whose preview URLs go nowhere.
  const PARTITION = 'preview-probe';
  const target = session.fromPartition(PARTITION);
  // The same one-line adapter the container installs: the pure answer becomes a
  // response, with no fallback and no second policy.
  target.protocol.handle(preview.PREVIEW_SCHEME, (request) => {
    const answer = preview.answerPreviewRequest(request.url, store);
    return new Response(answer.body, { status: answer.status, headers: answer.headers });
  });

  const htmlTicket = store.issue({ content: PREVIEW_HTML, name: 'report.html', kind: 'html' });
  // 1x1 PNG, so the inert-kind embedding is measured too.
  const pngTicket = store.issue({
    content: Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
      'base64',
    ),
    name: 'chart.png',
    kind: 'image',
  });

  const win = new BrowserWindow({
    show: false,
    width: 800,
    height: 600,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      partition: PARTITION,
    },
  });
  // A failed load is recorded rather than waited out: a probe that reports "the
  // frame never reported in" without saying *why* costs more time than it saves.
  const failures = [];
  win.webContents.on('did-fail-load', (_event, code, description, url) => {
    failures.push({ code, description, url });
  });
  win.webContents.on('console-message', (_event, level, message, line, sourceId) => {
    if (level >= 2) failures.push({ console: message, sourceId, line });
  });
  await win.loadURL(`${origin}/console`);

  check('the console page holds a bridge sentinel and a cookie',
    await inPage(win, 'typeof window.desktopHost === "object"') === true,
    'preconditions: the page is the surface with the bridge');

  // --- the frame: embeddable, scripted, and isolated -----------------------
  await inPage(win, `
    const frame = document.createElement('iframe');
    frame.id = 'preview';
    frame.setAttribute('sandbox', 'allow-scripts allow-forms allow-modals');
    frame.src = ${JSON.stringify(htmlTicket.url)};
    document.body.appendChild(frame);
    const image = document.createElement('img');
    image.id = 'preview-image';
    image.src = ${JSON.stringify(pngTicket.url)};
    window.__imageLoad = new Promise(function (resolve) {
      image.addEventListener('load', function () { resolve('loaded'); });
      image.addEventListener('error', function () { resolve('error'); });
    });
    document.body.appendChild(image);
    true;
  `);

  const facts = await waitFor(async () => {
    const message = await inPage(win, 'window.__messages.length ? window.__messages[0] : null');
    return message && message.data && message.data.facts ? message.data : null;
  }, { timeout: 10000 });

  check('a preview URL loads as a document and runs its own inline script',
    !!(facts && facts.facts && facts.facts.scriptRan === true),
    facts ? facts.facts.href : `the frame never reported in; failures=${JSON.stringify(failures)}`);
  check('normal content still renders under the policy',
    !!(facts && facts.facts.style === 'rgb(1, 2, 3)'),
    facts ? `computed background ${facts.facts.style}` : '');
  const frameOrigin = await inPage(win, 'window.__messages.length ? window.__messages[0].origin : null');
  check('the previewed document is a sandboxed, opaque origin',
    !!(facts && facts.facts.storage === 'blocked' && facts.facts.cookie === 'threw' && frameOrigin === 'null'),
    facts
      ? `the origin the console saw on the frame's message=${JSON.stringify(frameOrigin)} (opaque);`
        + ` its own location.origin=${JSON.stringify(facts.facts.origin)} (the URL tuple, not the effective origin);`
        + ` localStorage=${facts.facts.storage}, document.cookie=${JSON.stringify(facts.facts.cookie)}`
      : '');
  check('the previewed document sees no cookie from the console origin',
    !!(facts && facts.facts.cookie === 'threw'
      && (await inPage(win, 'document.cookie')).includes('console_session=super-secret')),
    facts
      ? `document.cookie in the frame=${JSON.stringify(facts.facts.cookie)}; in the console=${JSON.stringify(await inPage(win, 'document.cookie'))}`
      : '');
  check('the previewed document cannot reach its parent document',
    !!(facts && facts.facts.parentDom === 'blocked'),
    facts ? `parent.document=${facts.facts.parentDom}` : '');
  check('the previewed document cannot navigate the top window',
    !!(facts && facts.facts.topLocation === 'blocked'),
    facts ? `top.location=${facts.facts.topLocation}` : '');
  check('the previewed document has no native bridge of any name',
    !!(facts && !facts.facts.hasDesktopHost && !facts.facts.hasElectronApi
      && !facts.facts.hasRequire && !facts.facts.hasProcess
      && !facts.facts.hasModule && !facts.facts.hasIpcRenderer),
    facts ? JSON.stringify({
      desktopHost: facts.facts.hasDesktopHost,
      electronAPI: facts.facts.hasElectronApi,
      require: facts.facts.hasRequire,
      process: facts.facts.hasProcess,
      module: facts.facts.hasModule,
      ipcRenderer: facts.facts.hasIpcRenderer,
    }) : '');
  check('a preview frame is a frame and not the shell (the page still owns the top)',
    previewFrame(win) !== undefined && (await inPage(win, 'window.top === window')) === true,
    `frame url ${previewFrame(win) && previewFrame(win).url}`);

  // --- the frame's network reach ------------------------------------------
  const reach = previewFrame(win);
  const exfil = await reach.executeJavaScript(`
    (async function () {
      const answer = {};
      try { await fetch('${origin}/secret', { mode: 'no-cors' }); answer.http = 'reached'; }
      catch (e) { answer.http = 'blocked'; }
      try { await fetch('${preview.PREVIEW_SCHEME}://preview/anything', { mode: 'no-cors' }); answer.scheme = 'reached'; }
      catch (e) { answer.scheme = 'blocked'; }
      answer.websocket = await new Promise(function (resolve) {
        let settled = false;
        const finish = (value) => { if (!settled) { settled = true; resolve(value); } };
        try {
          const ws = new WebSocket('ws://127.0.0.1:${instance.address().port}/ws');
          ws.onopen = function () { finish('opened'); };
          ws.onerror = function () { finish('blocked'); };
          ws.onclose = function () { finish('blocked'); };
        } catch (e) { finish('blocked'); }
        setTimeout(function () { finish('blocked'); }, 800);
      });
      // A sibling file's preview: the ticket is a real one, so the only thing
      // that can refuse this is the policy (img-src data: blob:).
      answer.siblingImage = await new Promise(function (resolve) {
        const img = document.createElement('img');
        img.onload = function () { resolve('loaded'); };
        img.onerror = function () { resolve('error'); };
        img.src = ${JSON.stringify(pngTicket.url)};
        document.body.appendChild(img);
        setTimeout(function () { resolve('error'); }, 1500);
      });
      return answer;
    })()
  `, true);
  check('the previewed document cannot call the local server at all',
    exfil.http === 'blocked' && server.hits.length === 0,
    `fetch=${exfil.http}, WebSocket=${exfil.websocket}, and the server recorded ${JSON.stringify(server.hits)}`);
  check('the previewed document cannot fetch another preview resource',
    exfil.scheme === 'blocked',
    `cow-preview fetch=${exfil.scheme}`);
  check('the previewed document cannot even display another previewed file',
    exfil.siblingImage === 'error',
    `an <img> for a valid sibling ticket answered ${exfil.siblingImage}`);

  // --- the page's own reach ------------------------------------------------
  check('the console can embed the protected URL in an image',
    await inPage(win, 'window.__imageLoad') === 'loaded',
    'an inert kind has to render, or "正常获权内容仍可展示" is false');
  const readable = await inPage(win, `
    (async function () {
      try {
        const res = await fetch(${JSON.stringify(pngTicket.url)});
        return 'read ' + (await res.text()).length + ' bytes';
      } catch (e) { return 'refused'; }
    })()
  `);
  check('the console cannot read a preview\'s bytes into script',
    readable === 'refused',
    `fetch of a preview URL answered: ${readable}`);

  // --- the clock ------------------------------------------------------------
  store.revoke(pngTicket.token);
  const afterRevoke = await inPage(win, `
    (async function () {
      try { const res = await fetch(${JSON.stringify(pngTicket.url)}); return res.status; }
      catch (e) { return 'refused'; }
    })()
  `);
  const reembed = await inPage(win, `
    (async function () {
      return await new Promise(function (resolve) {
        const img = document.createElement('img');
        img.addEventListener('load', function () { resolve('loaded'); });
        img.addEventListener('error', function () { resolve('error'); });
        img.src = ${JSON.stringify(pngTicket.url)};
        document.body.appendChild(img);
      });
    })()
  `);
  check('a revoked preview URL is gone, from the page and from an element',
    (afterRevoke === 404 || afterRevoke === 'refused') && reembed === 'error',
    `fetch=${afterRevoke} <img>=${reembed}`);

  // --- what the server saw ---------------------------------------------------
  check('the console origin was never asked for anything but its own page',
    server.requests.every((entry) => entry === '/console'),
    JSON.stringify(server.requests));

  win.destroy();
  instance.close();
  return results;
}

app.whenReady()
  .then(main)
  .then((results) => {
    const failed = results.filter((entry) => !entry.ok);
    console.log(JSON.stringify({
      probe: 'summary',
      checks: results.length,
      failed: failed.length,
      verdict: failed.length ? 'FAIL' : 'PASS',
    }));
    app.exit(failed.length ? 1 : 0);
  })
  .catch((error) => {
    console.log(JSON.stringify({ probe: 'crashed', ok: false, detail: String(error && error.stack || error) }));
    app.exit(2);
  });
