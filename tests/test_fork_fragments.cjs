// Fork fragment mount-point contract (change fork-decoupling-and-tenant-hardening, task 8.8).
//
// Fork-specific markup used to live inline in chat.html, which made every
// upstream merge conflict on that region. It now lives in a standalone fragment
// under channel/web/static/fragments/ that a generic loader injects into a mount
// element. The mount element keeps the original id/classes/ARIA contract, and the
// fragment keeps every id/name/attribute that JS and the browser contract tests
// depend on. These assertions fail if either half of that contract regresses.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const read = p => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const { readWebLayer } = require('./_web_layer.cjs');
const chatHtml = read('channel/web/chat.html');
const loader = read('channel/web/static/js/fragments.js');
const fragment = read('channel/web/static/fragments/appearance-dialog.html');

test('chat.html keeps the appearance dialog mount point and drops the inline markup', () => {
    assert.match(chatHtml,
        /<dialog id="appearance-dialog" aria-labelledby="appearance-title" aria-describedby="appearance-scope"[\s\S]{0,200}?data-fork-fragment="assets\/fragments\/appearance-dialog\.html"><\/dialog>/);
    // The fork-only inner markup must no longer be inlined in the core file.
    assert.doesNotMatch(chatHtml, /id="appearance-options"/);
    assert.doesNotMatch(chatHtml, /name="web-palette"/);
});

test('the loader script is deferred and runs before console.js', () => {
    assert.match(loader, /window\.CowFragments\s*=/);
    assert.match(loader, /querySelectorAll\('\[data-fork-fragment\]'\)/);
    assert.match(loader, /typeof window\.applyI18n === 'function'/);
    const loaderIdx = chatHtml.indexOf('assets/js/fragments.js');
    const consoleIdx = chatHtml.indexOf('assets/js/console.js');
    assert.ok(loaderIdx >= 0, 'fragments.js script tag missing');
    assert.ok(consoleIdx >= 0, 'console.js script tag missing');
    assert.ok(loaderIdx < consoleIdx, 'fragments.js must load before console.js');
});

test('the fragment preserves the DOM contract JS and browser tests rely on', () => {
    for (const id of ['appearance-options', 'appearance-title', 'appearance-scope',
        'appearance-resolved', 'appearance-storage-warning', 'appearance-language-warning']) {
        assert.match(fragment, new RegExp(`id="${id}"`), `fragment lost #${id}`);
    }
    for (const name of ['web-palette', 'web-mode', 'web-language']) {
        assert.match(fragment, new RegExp(`name="${name}"`), `fragment lost input[name=${name}]`);
    }
    for (const key of ['appearance_title', 'appearance_scope', 'appearance_storage_failed',
        'appearance_reset', 'appearance_instant', 'account_prefs_lang']) {
        assert.match(fragment, new RegExp(`data-i18n="${key}"`),
            `fragment lost data-i18n=${key}`);
    }
    assert.match(fragment, /onclick="closeAppearancePreferences\(\)"/);
    assert.match(fragment, /onclick="CowAppearance\.reset\(\)"/);
});

test('the mount point is a documented, stable anchor', () => {
    // The comment is what tells a future upstream merge that the element is a
    // deliberate mount point, not dead markup it may delete. Without it the
    // anchor (and the fragment contract) can be silently lost.
    assert.match(chatHtml, /Fork fragment mount point[\s\S]{0,700}?data-fork-fragment=/,
        'the appearance-dialog mount point must carry its documenting comment');
});

test('the server stamps the fragment loader and the fragment markup', () => {
    // fragments.js fetches the fragment HTML at runtime, so both URLs have to
    // move with the bytes behind them: a page that keeps a stale loader or a
    // stale fragment will mount the previous markup. They have different
    // owners now that the page is assembled (change port-upstream-tasks-page):
    // the loader is a first-party script the assembler stamps like any other,
    // and the fragment is reached through a markup attribute the assembler
    // never scans, so the fork handler stamps it from the fragment directory.
    // The end-to-end half -- both URLs carry a stamp in the served page -- is
    // asserted where the handler can be driven: test_doc_edit.py.
    const server = readWebLayer();
    assert.match(chatHtml, /<script defer src="assets\/js\/fragments\.js"><\/script>/,
        'the loader must stay a first-party script reference for render() to stamp');
    assert.match(server, /fragments_dir = os\.path\.join\([^\n]*'static', 'fragments'\)/,
        'static/fragments must be discovered so its markup is stamped');
    assert.match(server, /f'\{reference\}\?v=\{version\}'/,
        'every discovered fragment the page declares must carry a version stamp');
});

test('a missing optional UI seam falls back to display only and never touches authorization', async () => {
    // R5's fourth form: rdai with only an *optional display* seam missing. The
    // fragment is presentation, so its absence may cost the appearance dialog
    // and must cost nothing else -- in particular it must not widen or narrow
    // any backend authorization, which is only true if the loader's failure
    // path is a no-op (no mount, no i18n pass, no event) and it reaches nothing
    // but the fragment URL it was declared with.
    const vm = require('node:vm');
    const allowed = [];
    const events = [];
    const el = {
        innerHTML: '<!-- mount left as declared -->',
        getAttribute: name => name === 'data-fork-fragment'
            ? 'assets/fragments/appearance-dialog.html' : null,
        removeAttribute: () => { throw new Error('a failed mount must keep its declaration'); },
    };
    const sandbox = {
        window: { applyI18n: () => { throw new Error('a failed mount must not localize'); } },
        document: {
            readyState: 'complete',
            querySelectorAll: () => [el],
            addEventListener: () => {},
            dispatchEvent: event => { events.push(event.type); },
        },
        fetch: url => {
            allowed.push(String(url));
            return Promise.resolve({ ok: false, status: 404, text: async () => '' });
        },
        CustomEvent: class { constructor(type) { this.type = type; } },
    };
    vm.createContext(sandbox);
    vm.runInContext(loader, sandbox);
    await sandbox.window.CowFragments.mountAll(sandbox.document);

    assert.ok(allowed.length > 0, 'the loader must attempt the declared fragment');
    assert.deepEqual([...new Set(allowed)], ['assets/fragments/appearance-dialog.html'],
        'the loader must reach nothing but the declared fragment');
    assert.equal(el.innerHTML, '<!-- mount left as declared -->',
        'a missing fragment must leave the mount point empty, not half-mounted');
    assert.deepEqual(events, [], 'no mount event may fire for markup that never landed');
    assert.doesNotMatch(loader, /\/api\//,
        'a display seam must not be able to reach a business endpoint');
    assert.doesNotMatch(loader, /credentials:\s*'include'|localStorage|sessionStorage/,
        'a display seam must not carry credentials or persist anything');
});
