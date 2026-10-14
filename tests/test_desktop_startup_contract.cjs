// The desktop startup contract, checked across the files that have to agree.
//
// Change ``harden-windows-desktop-relability``, tasks 2.3-2.5. Four separate
// places have to stay in step for a startup failure to reach the user as
// advice instead of as a code:
//
//   1. ``BackendErrorCode`` in the main process (what can go wrong),
//   2. the same union in the renderer (its mirror -- the file even says so),
//   3. ``CAUSE_COPY`` in StatusScreen (which sentence to show), and
//   4. the i18n dictionary, in both languages.
//
// A code that exists in only some of them renders as the generic "the app failed
// to start", which is the outcome this change exists to remove -- so the gap is
// a test failure here rather than a bug report later.
//
// Run: node --test tests/test_desktop_startup_contract.cjs

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');

// Normalized to LF: the repository mixes line endings, and every pattern below
// is written against the text rather than against one platform's newline.
function read(relative) {
    return fs.readFileSync(path.join(root, relative), 'utf8').replace(/\r\n/g, '\n');
}

const mainManager = read('desktop/src/main/python-manager.ts');
const rendererTypes = read('desktop/src/renderer/src/types.ts');
const statusScreen = read('desktop/src/renderer/src/components/StatusScreen.tsx');
const useBackend = read('desktop/src/renderer/src/hooks/useBackend.ts');
const i18n = read('desktop/src/renderer/src/i18n.ts');
const index = read('desktop/src/main/index.ts');
const setupScript = read('desktop/build/setup-windows.ps1');
const devScript = read('desktop/build/dev-windows.ps1');

function unionMembers(source, typeName) {
    const block = new RegExp(`export type ${typeName} =([\\s\\S]*?)\\n\\n`).exec(source);
    assert.ok(block, `${typeName} not found`);
    return [...block[1].matchAll(/'([a-z_]+)'/g)].map((match) => match[1]);
}

function causeCopy() {
    const block = /const CAUSE_COPY[\s\S]*?= \{([\s\S]*?)\n\}/.exec(statusScreen);
    assert.ok(block, 'CAUSE_COPY not found');
    const entries = new Map();
    for (const line of block[1].split('\n')) {
        const match = /^\s{2}([a-z_]+):\s*\{\s*desc:\s*'([^']+)',\s*hint:\s*'([^']+)'\s*\},?\s*$/.exec(line);
        if (match) entries.set(match[1], { desc: match[2], hint: match[3] });
    }
    return entries;
}

function dictionaryUses(key) {
    const occurrences = i18n.split(new RegExp(`\\n\\s+${key}:`, 'g')).length - 1;
    return occurrences;
}

test('the main process and the renderer describe the same failures', () => {
    const main = unionMembers(mainManager, 'BackendErrorCode');
    const renderer = unionMembers(rendererTypes, 'BackendErrorCode');
    assert.deepEqual([...renderer].sort(), [...main].sort(),
        'types.ts says it mirrors main/python-manager.ts; a code on one side only is a silent generic message');
    for (const code of ['python_not_found', 'python_invalid', 'dependency_missing']) {
        assert.ok(main.includes(code), `${code} is missing from the main-process contract`);
    }
});

test('every failure code has copy of its own', () => {
    const codes = unionMembers(rendererTypes, 'BackendErrorCode');
    const copy = causeCopy();
    for (const code of codes) {
        assert.ok(copy.has(code), `${code} has no CAUSE_COPY entry, so it would show the generic sentence`);
    }
    assert.deepEqual([...copy.keys()].sort(), [...codes].sort(), 'and nothing stale pointing at a removed code');
});

test('every sentence the failure screen can show exists in both languages', () => {
    for (const [code, keys] of causeCopy()) {
        for (const key of [keys.desc, keys.hint]) {
            assert.equal(dictionaryUses(key), 2, `${key} (for ${code}) must exist once in zh and once in en`);
        }
    }
});

test('failures that waiting cannot fix skip the retry deadline', () => {
    const block = /const UNRECOVERABLE_CODES[\s\S]*?= new Set<BackendErrorCode>\(\[([\s\S]*?)\]\)/.exec(useBackend);
    assert.ok(block, 'UNRECOVERABLE_CODES not found');
    for (const code of ['python_not_found', 'python_invalid', 'dependency_missing']) {
        assert.match(block[1], new RegExp(`'${code}'`),
            `${code} cannot improve by waiting, so it must not spend 40 seconds on a spinner`);
    }
});

// ---------------------------------------------------------------------------
// The Windows entry points (task 2.4) and remote mode (task 2.5)
// ---------------------------------------------------------------------------

test('the preparation script probes the interpreter and never overwrites a working setup', () => {
    // Comments are allowed to *mention* the files this must not touch; only the
    // executable lines are held to the rule.
    const code = setupScript.split('\n').filter((line) => !line.trim().startsWith('#')).join('\n');

    assert.match(code, /sys\.version_info/, 'the interpreter is probed, not trusted');
    assert.match(code, /requirements-desktop\.txt/, 'dependencies come from the pinned list');
    assert.match(code, /\.venv-win/, 'the Windows environment has its own directory');
    assert.match(code, /Microsoft Store/i, 'the Store alias is explained rather than passed through');

    // Preparation is additive: the macOS environment and the app's own data are
    // not this script's business, and it deletes nothing at all.
    assert.doesNotMatch(code, /\.venv(?!-win)/, 'the macOS .venv is never addressed');
    assert.doesNotMatch(code, /identity\.db|config\.json/, 'no business data is read or written');
    assert.doesNotMatch(code, /Remove-Item|rm\s+-/i, 'preparation never deletes anything');
});

test('the development launcher clears the variable that makes Electron run as Node', () => {
    const clearAt = devScript.indexOf('Remove-Item Env:\\ELECTRON_RUN_AS_NODE');
    const startAt = devScript.indexOf('& $electron .');
    assert.ok(clearAt !== -1, 'ELECTRON_RUN_AS_NODE is not cleared');
    assert.ok(startAt !== -1, 'Electron is not started');
    assert.ok(clearAt < startAt, 'the variable must be cleared before Electron is started');
    assert.match(devScript, /\.venv-win/, 'the prepared environment is put in front of PATH');
});

test('a remote install never probes for or starts a local Python', () => {
    // The probe belongs to the local backend, and the local backend is only
    // started on the non-remote branch -- so "remote needs no Python" is a
    // property of the wiring, not of the user's machine.
    const remoteBranch = /if \(startup\.mode === 'remote'\) \{([\s\S]*?)\n  \} else \{([\s\S]*?)\n  \}/.exec(index);
    assert.ok(remoteBranch, 'the startup mode branch was not found');
    assert.doesNotMatch(remoteBranch[1], /startBackend\(/, 'remote mode must not start the local backend');
    assert.match(remoteBranch[2], /await startBackend\(\)/, 'local mode must start it');
    assert.equal(index.split('await startBackend()').length - 1, 1, 'and only there');
});
