// The card-to-system-action probe (change
// align-desktop-project-execution-with-master, task 9.8, acceptance A25).
//
// Run:  desktop/node_modules/.bin/electron desktop/e2e/cards-and-system-open.probe.cjs
//
// "The user clicks the card and the file opens in their own application" is one
// gesture and four layers:
//
//   1. `workspace.js` renders the card and delegates the click;
//   2. `fork/project-source.js` (`CowProjectSource.act`) names the action and
//      adds the *live* workspace;
//   3. `fork/desktop-host.js` (`CowDesktopHost.projectAction`) turns that name
//      into the bridge method;
//   4. the preload carries it to the shell.
//
// The suites cover each layer against a stub of the next one, which is how four
// system actions shipped that were refused by name at layer 3 -- both ends
// individually correct, every click dead. So this probe loads the *shipping*
// scripts in the order the page loads them, in a real Chromium, and clicks the
// real buttons: the only thing stubbed is the preload underneath, and what it
// records is what the shell would have received.
//
// Two browser surfaces are stubbed, both because a probe must not block on a
// human: `_wsToast` (a function `console.js` owns; the message text it is given
// is the real one, read out of the shipped dictionaries) and `window.confirm`
// (a native modal). `escapeHtml` and `t` are the console's too, and are rebuilt
// here the way the console builds them -- `t` out of the *shipped* dictionaries,
// so every string a check compares against is the string the user would read.
//
// The probe prints one JSON line per check and exits non-zero if any failed.
// Its output is kept as evidence (see `evidence/p3-artifacts-and-actions.md`).

const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', '..');
const STATIC = path.join(ROOT, 'channel', 'web', 'static', 'js');

/** The shipping page modules, served under the paths the page expects. */
const FILES = {
  '/js/i18n/core.js': path.join(STATIC, 'i18n', 'core.js'),
  '/js/fork/desktop-host.js': path.join(STATIC, 'fork', 'desktop-host.js'),
  '/js/fork/project-source.js': path.join(STATIC, 'fork', 'project-source.js'),
  '/js/workspace.js': path.join(STATIC, 'workspace.js'),
};

const results = [];
function check(name, ok, detail) {
  results.push({ check: name, ok: !!ok, detail: detail === undefined ? '' : detail });
  console.log(JSON.stringify({ probe: name, ok: !!ok, detail: detail === undefined ? '' : detail }));
}

/**
 * The page: the real scripts, and a preload that answers like a container.
 *
 * `window.desktopHost` is the only invented surface. It declares the methods a
 * container declares (`getCapabilities` and friends, plus the four actions and
 * the preview), records every call, and takes its answers from `__answers` so a
 * check can script a refusal without touching the page code.
 */
const HARNESS = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>console</title></head>
<body>
<div id="messages"></div>
<script>
window.__calls = [];
window.__toasts = [];
var PROJECT_METHODS = [
  'projectSource', 'projectTree', 'projectSearch', 'projectResolve', 'projectRead', 'projectWrite'
];
var ACTION_METHODS = ['projectOpenFile', 'projectRevealFile', 'projectCopyPath', 'projectSaveFileAs'];
window.__answers = {};
function record(method, params) {
  window.__calls.push({ method: method, params: params || {} });
  var scripted = window.__answers[method];
  if (scripted) return Promise.resolve(scripted(params || {}));
  return Promise.resolve({ ok: true, name: 'summary.csv' });
}
var preload = {
  getCapabilities: function () {
    return Promise.resolve({
      bridge: '1.9.0',
      localFiles: true,
      methods: ['getCapabilities', 'suspendLocalContext', 'saveArtifact', 'openExternal',
        'onHostEvent'].concat(PROJECT_METHODS).concat(ACTION_METHODS).concat(['projectPreviewFile'])
    });
  },
  suspendLocalContext: function () { return Promise.resolve({}); },
  saveArtifact: function () { return Promise.resolve({}); },
  openExternal: function () { return Promise.resolve({ opened: true }); },
  onHostEvent: function () { return function () {}; },
  projectPreviewFile: function (params) { return record('projectPreviewFile', params); }
};
ACTION_METHODS.forEach(function (method) {
  preload[method] = function (params) { return record(method, params); };
});
window.desktopHost = preload;

// The one window surface a probe must not leave to a human: a native modal
// would block the harness with no way to answer it.
window.confirm = function () { return true; };
</script>
<script src="/js/i18n/core.js"></script>
<script src="/js/fork/desktop-host.js"></script>
<script src="/js/fork/project-source.js"></script>
<script src="/js/workspace.js"></script>
<script>
// "workspace.js" is a classic script: its helpers are global functions, and the
// card's click handler is delegated from "document".
(function build() {
  var languages = window.__cowI18N__ || {};
  var table = (languages.core && languages.core.zh) || {};
  var english = (languages.core && languages.core.en) || {};
  // The console's own lookup order (console.js): current language, then English,
  // then the key itself. Driven with the Chinese dictionary so a missing string
  // is visible here.
  window.t = function (key) { return table[key] || english[key] || key; };
  // "console.js" owns these two, and the real page gets them from there: the
  // escaping is the console's own (a DOM text node's HTML), and the toast is
  // the console's toast. Copied here, rather than imported, because the console
  // is a 13k-line page script that cannot be loaded without a full backend.
  window.escapeHtml = function (value) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(value === undefined || value === null ? '' : String(value)));
    return div.innerHTML;
  };
  window.__toasts = [];
  window._wsToast = function (message) { window.__toasts.push(String(message)); };
  window.__mounted = [];
  window.mountCard = function (meta) {
    var host = document.getElementById('messages');
    host.innerHTML = window.renderFileCard(meta);
    window.__mounted.push(host.firstElementChild);
    return host.firstElementChild;
  };
  window.clickCard = function (action) {
    var card = window.__mounted[window.__mounted.length - 1];
    var button = card && card.querySelector('[data-action="' + action + '"]');
    if (!button) return false;
    button.click();
    return true;
  };
  // The panel sets this when it opens a local project; the adapter reads it on
  // every call, so a replayed card's own project can be compared with it.
  window.wsCurrentSource = 'desktop';
  if (window.CowProjectSource) {
    window.CowProjectSource.configure({
      binding: function () { return { workspace_id: 'ws_probe', binding_id: 'b_probe' }; },
      kindOf: function () { return 'csv'; },
      editable: function () { return true; },
      previewable: function () { return true; }
    });
  }
  window.__ready = true;
})();
</script>
</body></html>`;

/** A card as the panel renders one for a file on this machine (task 9.4). */
const LOCAL_CARD = {
  file_name: 'summary.csv',
  rel_path: 'out/summary.csv',
  kind: 'csv',
  local: true,
  resolution: 'ok',
};
/** The same file, as a server-rebuilt history card carries it (task 9.6). */
const REPLAYED_CARD = Object.assign({}, LOCAL_CARD, { workspace_id: 'ws_probe' });
/** A card the server already reported as gone. */
const GONE_CARD = Object.assign({}, REPLAYED_CARD, { resolution: 'missing' });
/** A card whose file was produced in a project that is not the one open now. */
const OTHER_PROJECT_CARD = Object.assign({}, REPLAYED_CARD, { workspace_id: 'ws_other' });
/** A file the server owns: the panel must keep offering its old behaviour. */
const SERVER_CARD = {
  file_name: 'server.csv',
  rel_path: 'reports/server.csv',
  kind: 'csv',
  raw_url: '/api/workspace/raw?path=reports/server.csv',
};

app.whenReady()
  .then(main)
  .then(() => app.exit(0))
  .catch((error) => {
    console.log(JSON.stringify({ probe: 'crashed', ok: false,
      detail: String((error && error.stack) || error) }));
    app.exit(2);
  });

async function main() {
  const server = http.createServer((req, res) => {
    try {
      if (req.url === '/page.html') {
        res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
        res.end(HARNESS);
        return;
      }
      const file = FILES[req.url];
      if (!file) {
        res.writeHead(404).end('not found');
        return;
      }
      res.writeHead(200, { 'content-type': 'application/javascript; charset=utf-8' });
      res.end(fs.readFileSync(file));
    } catch (error) {
      // A script that is never served leaves the page waiting forever, which
      // looks exactly like a hang: say what happened instead.
      console.log(JSON.stringify({ probe: 'serve-failed', ok: false,
        detail: `${req.url} ${String((error && error.message) || error)}` }));
      try { res.writeHead(500).end('serve failed'); } catch (_) { /* already sent */ }
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;

  const win = new BrowserWindow({
    show: false,
    width: 1100,
    height: 800,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  // A page that never loads is recorded rather than waited out.
  win.webContents.on('did-fail-load', (_event, code, description, url) => {
    console.log(JSON.stringify({ probe: 'load-failed', ok: false,
      detail: `${code} ${description} ${url}` }));
  });
  await win.loadURL(`${origin}/page.html`);

  // An error thrown in the page comes back as Electron's own opaque message, so
  // both helpers carry the page's stack out with them.
  const run = async (expression) => {
    const answer = await win.webContents.executeJavaScript(
      `(function () { try { return { value: (${expression}) }; }
        catch (error) { return { error: String((error && (error.stack || error.message)) || error) }; } })()`,
      true);
    if (answer && answer.error) throw new Error(answer.error);
    return answer ? answer.value : undefined;
  };
  const runProgram = async (source) => {
    const answer = await win.webContents.executeJavaScript(
      `(function () { try { ${source}; return { ok: true }; }
        catch (error) { return { ok: false, error: String((error && (error.stack || error.message)) || error) }; } })()`,
      true);
    if (!answer || !answer.ok) throw new Error((answer && answer.error) || 'the page script failed');
  };
  /** The shipped string for a key, in the language the page is running. */
  const string = (key) => run(`(window.__cowI18N__.core.zh || {})[${JSON.stringify(key)}]`);

  // -- 1. the shipping modules load together -----------------------------
  const loaded = await run(`({
    ready: !!window.__ready,
    host: typeof CowDesktopHost,
    source: typeof CowProjectSource,
    card: typeof renderFileCard
  })`);
  check('the page modules load in one document',
    loaded.ready && loaded.host === 'object' && loaded.source === 'object'
      && loaded.card === 'function', JSON.stringify(loaded));

  // -- 2. the four actions are drawn for a local file --------------------
  const wanted = ['local-open', 'local-reveal', 'local-copy-path', 'local-save-as'];
  const localHtml = await run(`window.renderFileCard(${JSON.stringify(LOCAL_CARD)})`);
  check('a local card carries the four system actions',
    wanted.every((action) => localHtml.includes(`data-action="${action}"`)),
    wanted.filter((action) => !localHtml.includes(`data-action="${action}"`)).join(','));
  check('a local card offers no server download',
    !localHtml.includes('data-action="download"'),
    localHtml.includes('data-action="download"') ? 'a download button is drawn' : '');

  const serverHtml = await run(`window.renderFileCard(${JSON.stringify(SERVER_CARD)})`);
  check('a server card keeps the download and gets no system action',
    serverHtml.includes('data-action="download"')
      && !wanted.some((action) => serverHtml.includes(`data-action="${action}"`)));

  // -- 3. one real click, per action, all the way down -------------------
  const clicks = [
    ['local-open', 'projectOpenFile', 'ws_local_opened'],
    ['local-reveal', 'projectRevealFile', 'ws_local_revealed'],
    ['local-copy-path', 'projectCopyPath', 'ws_local_copied'],
  ];
  for (const [button, method, key] of clicks) {
    await runProgram(`window.__calls = []; window.__toasts = []; window.__answers = {};
      window.mountCard(${JSON.stringify(LOCAL_CARD)});
      window.clickCard('${button}')`);
    // The delegation, the adapter and the bridge are asynchronous.
    await new Promise((resolve) => setTimeout(resolve, 80));
    const calls = await run('window.__calls');
    check(`a click on ${button} reaches the shell as ${method}`,
      calls.length === 1 && calls[0].method === method
        && calls[0].params.workspace_id === 'ws_probe'
        && calls[0].params.path === 'out/summary.csv',
      JSON.stringify(calls));
    const toasts = await run('window.__toasts.slice()');
    const expectedText = await string(key);
    check(`${button} tells the user what happened, in their language`,
      toasts.length === 1 && toasts[0] === expectedText,
      JSON.stringify({ key, expectedText, toasts }));
  }

  // -- 4. save-as, the whole two-step path, from one click ---------------
  await runProgram(`window.__calls = []; window.__toasts = [];
    window.__answers = { projectSaveFileAs: function (params) {
      if (params.accept_current) return { ok: true, saved: true, name: 'summary.csv' };
      return { ok: false, code: 'changed', message: 'the file changed since it was read' };
    } };
    window.mountCard(Object.assign({}, ${JSON.stringify(LOCAL_CARD)}, { mtime: 1700000000 }));
    window.clickCard('local-save-as')`);
  await new Promise((resolve) => setTimeout(resolve, 150));
  const savedCalls = await run('window.__calls');
  const savedToasts = await run('window.__toasts.slice()');
  const savedText = await string('ws_local_saved_as');
  check('a changed file is asked about, then copied with the user\'s answer',
    savedCalls.length === 2
      && savedCalls[0].method === 'projectSaveFileAs'
      && savedCalls[0].params.expected_mtime === 1700000000
      && savedCalls[1].params.expected_mtime === 1700000000
      && savedCalls[1].params.accept_current === true
      && savedToasts.length === 1 && savedToasts[0] === savedText,
    JSON.stringify({ savedCalls, savedToasts }));

  // -- 5. a card the server said is gone is never asked about ------------
  await runProgram(`window.__calls = []; window.__toasts = [];
    window.mountCard(${JSON.stringify(GONE_CARD)});
    window.clickCard('local-open')`);
  await new Promise((resolve) => setTimeout(resolve, 80));
  const goneCalls = await run('window.__calls');
  const goneToasts = await run('window.__toasts.slice()');
  const goneText = await string('ws_local_file_gone');
  check('a card marked missing is refused from the card, and the shell is not bothered',
    goneCalls.length === 0 && goneToasts.length === 1 && goneToasts[0] === goneText,
    JSON.stringify({ goneCalls, goneToasts, goneText }));

  // -- 6. a card from another project carries its own project -------------
  await runProgram(`window.__calls = []; window.__toasts = [];
    window.mountCard(${JSON.stringify(OTHER_PROJECT_CARD)});
    window.clickCard('local-open')`);
  await new Promise((resolve) => setTimeout(resolve, 80));
  const otherCalls = await run('window.__calls');
  const otherToasts = await run('window.__toasts.slice()');
  const otherText = await string('ws_local_other_project');
  check('a card from another project is refused, without asking a same-named file',
    otherCalls.length === 0 && otherToasts.length === 1 && otherToasts[0] === otherText,
    JSON.stringify({ otherCalls, otherToasts, otherText }));

  // -- 7. the shell's own refusal is what the user reads -----------------
  await runProgram(`window.__calls = []; window.__toasts = [];
    window.__answers = { projectOpenFile: function () {
      return { ok: false, code: 'no_application',
        message: 'There is no application set to open the document' };
    } };
    window.mountCard(${JSON.stringify(LOCAL_CARD)});
    window.clickCard('local-open')`);
  await new Promise((resolve) => setTimeout(resolve, 80));
  const refusedCalls = await run('window.__calls');
  const refusedToasts = await run('window.__toasts.slice()');
  const noAppText = await string('ws_local_no_application');
  check('the shell\'s own reason reaches the user verbatim',
    refusedCalls.length === 1 && refusedToasts.length === 1
      && refusedToasts[0].indexOf(noAppText) === 0
      && refusedToasts[0].includes('There is no application set to open the document'),
    JSON.stringify({ refusedToasts, noAppText }));

  // -- 8. the preview seam, over the same adapter ------------------------
  await runProgram(`window.__calls = [];
    window.__answers = { projectPreviewFile: function () {
      return { ok: true, name: 'report.html', kind: 'html', size: 12, url: 'cow-preview://t/1' };
    } };
    window.__preview = null;
    CowProjectSource.preview('reports/report.html').then(function (reply) {
      window.__preview = reply;
    })`);
  await new Promise((resolve) => setTimeout(resolve, 80));
  const previewCalls = await run('window.__calls');
  const preview = await run('window.__preview');
  check('a preview goes to the host as projectPreviewFile and returns an embeddable URL',
    previewCalls.length === 1 && previewCalls[0].method === 'projectPreviewFile'
      && previewCalls[0].params.path === 'reports/report.html'
      && preview && preview.url === 'cow-preview://t/1',
    JSON.stringify({ previewCalls, preview }));

  // -- 9. what the shell was never asked, through this whole page ---------
  const asked = await run(`window.__calls.map(function (call) { return call.method; })`);
  check('nothing but the four actions and the preview was ever called',
    asked.every((method) => ['projectOpenFile', 'projectRevealFile', 'projectCopyPath',
      'projectSaveFileAs', 'projectPreviewFile'].indexOf(method) >= 0)
      && asked.length === 1,
    JSON.stringify(asked));

  const failed = results.filter((result) => !result.ok);
  console.log(JSON.stringify({ probe: 'summary', checks: results.length, failed: failed.length,
    verdict: failed.length ? 'FAIL' : 'PASS' }));
  win.destroy();
  await new Promise((resolve) => server.close(resolve));
  if (failed.length) throw new Error(`${failed.length} of ${results.length} checks failed`);
}
