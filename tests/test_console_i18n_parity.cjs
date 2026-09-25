// i18n namespace parity (change fork-decoupling-and-tenant-hardening, task 8.5).
//
// console.js used to carry one giant I18N literal extended by Object.assign
// blocks, which forced fork and upstream key insertions into the same lines and
// conflicted on every merge. The dictionaries now live in per-domain namespace
// files under static/js/i18n/ and merge at load time. This test is the proof the
// extraction is lossless:
//   1. the merged namespace table deep-equals the pre-split snapshot fixture;
//   2. namespaces do not overlap (each key has exactly one owner);
//   3. every namespace is complete in every language it declares;
//   4. console.js itself no longer carries the dictionaries.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const I18N_DIR = path.join(ROOT, 'channel/web/static/js/i18n');
const CONSOLE = path.join(ROOT, 'channel/web/static/js/console.js');
const CHAT_HTML = path.join(ROOT, 'channel/web/chat.html');
const FIXTURE = path.join(__dirname, 'fixtures/console_i18n_snapshot.json');

function namespaceFiles() {
    assert.ok(fs.existsSync(I18N_DIR), 'the per-domain i18n directory exists');
    const files = fs.readdirSync(I18N_DIR).filter(f => f.endsWith('.js')).sort();
    assert.ok(files.length >= 2, 'the i18n table is split across at least two namespace files');
    return files;
}

function loadRegistry() {
    const ctx = { window: {} };
    vm.createContext(ctx);
    for (const file of namespaceFiles()) {
        vm.runInContext(fs.readFileSync(path.join(I18N_DIR, file), 'utf8'), ctx, { filename: file });
    }
    const registry = ctx.window.__cowI18N__;
    assert.ok(registry && typeof registry === 'object' && !Array.isArray(registry),
        'namespace files register window.__cowI18N__ as an object keyed by domain');
    return registry;
}

function merge(registry) {
    const merged = {};
    for (const domain of Object.keys(registry)) {
        for (const lang of Object.keys(registry[domain])) {
            Object.assign(merged[lang] || (merged[lang] = {}), registry[domain][lang]);
        }
    }
    return merged;
}

const fixture = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
const registry = loadRegistry();
const merged = merge(registry);

test('every namespace declares at least one non-empty language', () => {
    const domains = Object.keys(registry);
    assert.ok(domains.length >= 2, 'at least two domains');
    for (const domain of domains) {
        const langs = Object.keys(registry[domain]);
        assert.ok(langs.length > 0, `${domain} declares at least one language`);
        for (const lang of langs) {
            assert.ok(Object.keys(registry[domain][lang]).length > 0,
                `${domain}/${lang} is non-empty`);
        }
    }
});

test('the merged namespace table deep-equals the pre-split snapshot', () => {
    assert.deepStrictEqual(merged, fixture,
        'the split must not add, drop, rename, or alter any translation');
});

test('no key overlaps between namespaces', () => {
    const owner = new Map();
    for (const domain of Object.keys(registry)) {
        const keys = new Set();
        for (const lang of Object.keys(registry[domain])) {
            for (const key of Object.keys(registry[domain][lang])) keys.add(key);
        }
        for (const key of keys) {
            assert.ok(!owner.has(key),
                `key ${key} is declared by both ${owner.get(key)} and ${domain}`);
            owner.set(key, domain);
        }
    }
});

test('each namespace declares the same keys in every language it uses', () => {
    // The snapshot is authoritative: zh-Hant is missing a handful of keys that
    // zh/en have, so completeness is measured against the snapshot per language.
    for (const domain of Object.keys(registry)) {
        const domainKeys = new Set();
        for (const lang of Object.keys(registry[domain])) {
            for (const key of Object.keys(registry[domain][lang])) domainKeys.add(key);
        }
        for (const lang of Object.keys(registry[domain])) {
            const actual = Object.keys(registry[domain][lang]).sort();
            const expected = [...domainKeys].filter(k => k in fixture[lang]).sort();
            assert.deepStrictEqual(actual, expected,
                `${domain}/${lang} must carry exactly the snapshot keys for that language`);
        }
    }
});

test('console.js no longer carries a domain dictionary itself', () => {
    const source = fs.readFileSync(CONSOLE, 'utf8');
    assert.ok(!/const I18N\s*=\s*\{/.test(source),
        'console.js no longer declares the inline I18N literal');
    assert.ok(!/Object\.assign\(\s*I18N/.test(source),
        'console.js no longer extends I18N with Object.assign blocks');
    // Representative fork and upstream domain keys must not be defined here.
    for (const key of ['tenant_channel_desc', 'todo_title', 'extid_title',
        'tasks_tab_records', 'appearance_title', 'home_greeting']) {
        assert.ok(!new RegExp('\\b' + key + '\\s*:').test(source),
            `console.js must not define the domain key ${key}`);
    }
});

test('every i18n namespace loads before console.js merges the table', () => {
    // console.js snapshots window.__cowI18N__ into its `I18N` lookup exactly once,
    // when the file executes (see mergeI18nNamespaces). A namespace registered
    // after that point still *exists* — the catalogs are complete and every other
    // assertion here passes — but every lookup falls through to the key itself,
    // so the page renders "audit_title" instead of "审计日志". Document order in
    // chat.html is therefore part of this contract, not a style choice.
    const html = fs.readFileSync(CHAT_HTML, 'utf8');
    const consoleAt = html.indexOf('assets/js/console.js');
    assert.ok(consoleAt > 0, 'chat.html loads assets/js/console.js');
    const refs = [...html.matchAll(/src="(assets\/js\/i18n\/[^"?]+)/g)].map(m => m[1]);
    assert.ok(refs.length >= 2, 'chat.html loads the per-domain i18n namespaces');
    for (const ref of refs) {
        assert.ok(html.indexOf(ref) < consoleAt,
            `${ref} must be loaded before assets/js/console.js merges the i18n table`);
    }
});
