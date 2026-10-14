// The memory view's advice must match the failure it is given.
//
// Change ``harden-windows-desktop-reliability``, task 3.3. The view used one
// line for every failure -- "refresh to retry, or sign in again if your session
// expired" -- so a security refusal told the reader to sign in, which cannot
// change it, and a held lock looked like a replaced directory (2026-10-13
// verification report, section 4.3). The mapping is data, so it is checked as
// data: every code the server can send has an answer, and the refusal's answer
// never mentions signing in.
//
// Run: node --test tests/test_memory_error_hints.cjs

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const view = fs.readFileSync(path.join(root, 'channel/web/static/js/fork/views/memory.js'), 'utf8');
const consoleCodes = fs.readFileSync(path.join(root, 'channel/web/memory_console.py'), 'utf8');
const personal = fs.readFileSync(path.join(root, 'agent/memory/personal.py'), 'utf8');

function hintTable() {
    const block = /const MEMORY_HINTS = \{([\s\S]*?)\n\};/.exec(view);
    assert.ok(block, 'MEMORY_HINTS not found in the memory view');
    const table = {};
    for (const line of block[1].split('\n')) {
        const match = /^\s*([a-z_]+):\s*'([a-z_]+)',?\s*$/.exec(line);
        if (match) table[match[1]] = match[2];
    }
    return table;
}

function dictionary() {
    const previous = global.window;
    global.window = {};
    try {
        require(path.join(root, 'channel/web/static/js/i18n/memory-account.js'));
        return (global.window.__cowI18N__ || {})['memory-account'] || {};
    } finally {
        global.window = previous;
    }
}

/** Every machine code the memory surface can answer with. */
function serverCodes() {
    const codes = new Set();
    for (const source of [consoleCodes, personal]) {
        for (const match of source.matchAll(/code\s*=\s*["']([a-z_]+)["']/g)) codes.add(match[1]);
        for (const match of source.matchAll(/CODE_[A-Z_]+\s*=\s*"([a-z_]+)"/g)) codes.add(match[1]);
    }
    return [...codes];
}

test('the failure path actually consults the table', () => {
    assert.match(view, /memoryShowState\(error\.message \|\| t\('memory_load_failed'\), memoryHintFor\(error\)/,
        'the view must choose its hint from the failure code, not from one generic line');
    assert.match(view, /error\.code = data\.code \|\| ''/,
        'the server code has to survive into the Error the view inspects');
});

test('the answers that matter are the answers that are mapped', () => {
    const table = hintTable();
    assert.equal(table.unsafe_path, 'memory_hint_refused', 'a refusal is not a session problem');
    assert.equal(table.busy, 'memory_hint_busy', 'a held lock is transient');
    assert.equal(table.no_identity, 'memory_hint_identity', 'a real identity failure may say so');
    assert.equal(table.unauthorized, 'memory_hint_identity');
    assert.equal(table.memory_unavailable, 'memory_hint_busy');
});

test('every code the server can send has an answer', () => {
    const table = hintTable();
    const known = new Set([
        ...Object.keys(table),
        // Codes whose advice is genuinely the generic retry line: the view falls
        // back to it on purpose rather than pretending to know more.
        'stale_revision', 'revision_required', 'not_found', 'unknown_category',
        'invalid_content', 'invalid_scope', 'clear_incomplete', 'scope_unavailable',
        'capability_disabled', 'forbidden', 'invalid_entry', 'error',
        // Request-shape codes: the caller asked for something the surface does
        // not address (an unknown scope, agent, entry or action). The generic
        // line is the honest answer, and listing them here is what keeps this
        // test about *failures* rather than about the request grammar.
        'unknown_scope', 'ambiguous_target', 'unknown_agent', 'unknown_entry',
        'entry_required', 'invalid_paging', 'unknown_action', 'index_pending',
    ]);
    const unmapped = serverCodes().filter((code) => !known.has(code));
    assert.deepEqual(unmapped, [], `these codes would show the generic line silently: ${unmapped.join(', ')}`);
});

test('every hint exists in every language the console ships', () => {
    const langs = dictionary();
    const keys = new Set(Object.values(hintTable()));
    assert.ok(keys.size >= 3, 'expected at least the refusal, busy and identity answers');
    for (const [lang, table] of Object.entries(langs)) {
        for (const key of keys) {
            assert.ok(table[key], `${key} is missing from ${lang}`);
        }
    }
});

test('a security refusal never tells the reader to sign in again', () => {
    const langs = dictionary();
    for (const [lang, table] of Object.entries(langs)) {
        const text = String(table.memory_hint_refused || '');
        assert.ok(text.length > 0, `memory_hint_refused is empty in ${lang}`);
        assert.doesNotMatch(text, /重新登录|重新登入|sign in again|log ?in again/i,
            `${lang}: a refusal must not suggest signing in`);
        assert.match(text, /安全|security|安全/i, `${lang}: and it should say the refusal was about safety`);
    }
});
