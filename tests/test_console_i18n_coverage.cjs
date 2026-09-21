// Every i18n key the console asks for must exist in the table the page merges.
//
// The split console (task 8.5) keeps its dictionaries in `static/js/i18n/*.js`
// and merges them at load; `static/js/core/i18n.js` holds the pre-split table
// and is loaded by no template at all. Adding a key to the wrong one of those
// two files is silent: the parity test still passes (it only compares the
// namespaces with the snapshot), the console still runs, and the user sees the
// raw key where a label belongs. That is exactly what happened to the coding
// Agent form, so this test asks the only question that matters at runtime --
// does `t(key)` resolve? -- for the keys that resolve from the markup and the
// scripts the page actually loads.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const WEB = path.join(ROOT, 'channel/web');
const STATIC = path.join(WEB, 'static');
const I18N_DIR = path.join(STATIC, 'js/i18n');
const PAGE = 'chat.html';

//: The assembler's marker (`channel/web/core/template.py`), resolved against
//: the web root.
const INCLUDE = /<!--#include\s+([^\s>]+?)\s*-->/g;

/**
 * The page as the browser receives it: the shell plus every fragment it
 * includes. Reading `chat.html` alone would quietly stop checking the keys the
 * fragments' markup binds -- the ported tasks view moved out of the shell, so
 * its labels resolve only if this follows it.
 */
function assembledPage(relative = PAGE, depth = 0) {
    assert.ok(depth < 8, 'include cycle');
    const text = fs.readFileSync(path.join(WEB, relative), 'utf8');
    return text.replace(INCLUDE, (_, fragment) => assembledPage(fragment, depth + 1));
}

//: The languages a user can pick. The lookup falls back to `en`, so a key that
//: exists only in one of them is still a bug: the other locale shows the key.
const REQUIRED_LANGUAGES = ['zh', 'en'];

//: Keys that concatenate a literal prefix with a runtime value
//: (`t('models_search_' + kind)`) cannot be resolved statically.
const DYNAMIC_SUFFIX = '_';

//: Pre-existing gaps, not this change's: `identity-admin.js` asks for
//: `admin_loading` and `functional-scheduler.js` / `views/models.js` ask for
//: `delete`, and no namespace defines either, so those controls render the raw
//: key. Listed here rather than fixed so this test stays a statement about the
//: coding work and the gap stays visible.
const KNOWN_MISSING = new Set(['admin_loading', 'delete']);

function mergedTable() {
    const context = { window: {} };
    vm.createContext(context);
    for (const file of fs.readdirSync(I18N_DIR).filter(f => f.endsWith('.js')).sort()) {
        vm.runInContext(fs.readFileSync(path.join(I18N_DIR, file), 'utf8'), context,
            { filename: file });
    }
    const registry = context.window.__cowI18N__;
    assert.ok(registry, 'the namespace files register window.__cowI18N__');
    const merged = {};
    for (const domain of Object.keys(registry)) {
        for (const lang of Object.keys(registry[domain])) {
            Object.assign(merged[lang] || (merged[lang] = {}), registry[domain][lang]);
        }
    }
    return merged;
}

/** The scripts the assembled page loads, in the order it loads them. */
function pageScripts() {
    return [...assembledPage().matchAll(/<script[^>]+src="assets\/(js\/[^"?]+)/g)].map(m => m[1]);
}

/** Keys asked for by name from the markup: data-i18n, -tip, -placeholder, -title. */
function keysFromMarkup() {
    return [...assembledPage().matchAll(/data-i18n(?:-[a-z]+)?="([^"]+)"/g)].map(m => m[1]);
}

/** Keys asked for from code: t('literal'). */
function keysFromScripts() {
    const found = new Set();
    for (const relative of pageScripts()) {
        const file = path.join(STATIC, relative);
        if (!fs.existsSync(file)) continue;
        const source = fs.readFileSync(file, 'utf8');
        for (const match of source.matchAll(/\bt\(\s*'([A-Za-z0-9_.\-]+)'/g)) {
            found.add(match[1]);
        }
        for (const match of source.matchAll(/\bt\(\s*"([A-Za-z0-9_.\-]+)"/g)) {
            found.add(match[1]);
        }
    }
    return found;
}

function unresolved(keys) {
    const merged = mergedTable();
    const missing = [];
    for (const key of keys) {
        if (key.endsWith(DYNAMIC_SUFFIX) || KNOWN_MISSING.has(key)) continue;
        for (const lang of REQUIRED_LANGUAGES) {
            if (!merged[lang][key]) missing.push(`${lang}:${key}`);
        }
    }
    return missing.sort();
}

test('every i18n key the page asks for from the markup resolves', () => {
    const keys = keysFromMarkup();
    assert.ok(keys.length > 100, `expected the page to bind many keys, saw ${keys.length}`);
    assert.deepEqual(unresolved(keys), []);
});

test('every i18n key the loaded scripts ask for by name resolves', () => {
    const keys = [...keysFromScripts()];
    assert.ok(keys.length > 100, `expected the scripts to ask for many keys, saw ${keys.length}`);
    assert.deepEqual(unresolved(keys), []);
});

test('the coding Agent surface is translated, not shown as keys', () => {
    // The concrete regression this file exists for: the type selector, the
    // project directory and the service row on the create form and the detail
    // pane. Spelled out so a future move between namespace files cannot pass by
    // deleting one of them.
    const keys = [
        'agents_type', 'agents_type_normal', 'agents_type_coding',
        'agents_type_hint', 'agents_type_locked',
        'agents_coding_project', 'agents_coding_project_placeholder',
        'agents_coding_project_hint', 'agents_coding_project_required',
        'agents_coding_service', 'agents_coding_service_unset',
        'agents_coding_service_hint',
        'coding_team_unavailable', 'coding_open_hint',
    ];
    assert.deepEqual(unresolved(keys), []);
    assert.deepEqual(keysFromMarkup().filter(k => unresolved([k]).length), []);
});

test('the pre-split table is not mistaken for the runtime table', () => {
    // `static/js/core/i18n.js` defines a second `I18N` literal that no template
    // loads. If a page ever starts loading it, or the namespaces move back into
    // it, this test's whole premise changes -- so the premise is asserted.
    const legacy = fs.readFileSync(path.join(STATIC, 'js/core/i18n.js'), 'utf8');
    assert.match(legacy, /const I18N = \{/, 'the pre-split table is where we think it is');
    assert.deepEqual(pageScripts().filter(s => s.includes('js/core/')), [],
        'no page script comes from the pre-split directory');
});
