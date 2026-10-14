// The local-source refusals must stay apart, and stay the same everywhere.
//
// Change ``harden-windows-desktop-reliability``, tasks 4.2/4.3. Three different
// paths decode a refusal that came *from this device* -- a workspace read
// (``wsErrorMessage``), a preview (``wsLocalPreviewMessage``) and a local action
// such as "open in Word" (``wsLocalActionMessage``) -- and all three used to say
//
//     if (code === 'device_offline' || code === 'device_error') ...
//
// so a device whose file helper had failed was reported as offline: the user was
// told to reconnect a device that was connected, and the actual fix (install or
// rebuild the app that ships the helper) was never mentioned. The 2026-10-13
// report has this as section 4.3.
//
// Run: node --test tests/test_workspace_local_refusals.cjs

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const workspace = fs.readFileSync(path.join(root, 'channel/web/static/js/workspace.js'), 'utf8');
const desktopIpc = fs.readFileSync(
    path.join(root, 'desktop/src/main/remote/remote-container-ipc.ts'), 'utf8');

function table() {
    const block = /const WS_LOCAL_REFUSAL_KEYS = \{([\s\S]*?)\n\};/.exec(workspace);
    assert.ok(block, 'WS_LOCAL_REFUSAL_KEYS not found in workspace.js');
    const parsed = {};
    for (const line of block[1].split('\n')) {
        const match = /^\s*([a-z_]+):\s*'([a-z_]+)',?\s*$/.exec(line);
        if (match) parsed[match[1]] = match[2];
    }
    return parsed;
}

function dictionary() {
    const previous = global.window;
    global.window = {};
    try {
        require(path.join(root, 'channel/web/static/js/i18n/core.js'));
        return (global.window.__cowI18N__ || {})['core'] || {};
    } finally {
        global.window = previous;
    }
}

test('all three decoding paths use the one table', () => {
    // Three call sites, plus the declaration itself, which the same pattern
    // matches (`function wsLocalRefusalText(code) {`).
    const calls = (workspace.match(/wsLocalRefusalText\(code\)/g) || []).length - 1;
    assert.equal(calls, 3, 'expected the read, preview and action paths to share the table');
    assert.doesNotMatch(workspace, /code === 'device_offline' \|\| code === 'device_error'/,
        'the collapsed mapping is the defect this change fixes');
});

test('the situations that need different advice have different answers', () => {
    const answers = table();
    const four = ['device_offline', 'device_error', 'helper_missing', 'platform_unsupported'];
    for (const code of four) assert.ok(answers[code], `${code} has no answer`);
    const keys = four.map((code) => answers[code]);
    assert.equal(new Set(keys).size, four.length,
        `these must not share a sentence: ${four.map((c) => `${c}->${answers[c]}`).join(', ')}`);
});

test('only a genuinely offline device is called offline', () => {
    const answers = table();
    const offline = answers.device_offline;
    for (const code of ['device_error', 'helper_missing', 'platform_unsupported']) {
        assert.notEqual(answers[code], offline, `${code} must not use the offline sentence`);
    }
});

test('the device reports a missing helper as its own code', () => {
    assert.match(desktopIpc, /code: 'helper_missing'/,
        'the fs-guard-missing refusal must carry a code, not a bare Error: a bare one '
        + 'reaches the console as device_error');
    assert.match(desktopIpc, /is not installed in this build/);
});

test('every local refusal the table names exists in every language', () => {
    const langs = dictionary();
    const keys = new Set(Object.values(table()));
    assert.ok(keys.size >= 8, 'expected the whole local-refusal family');
    for (const [lang, texts] of Object.entries(langs)) {
        for (const key of keys) assert.ok(texts[key], `${key} is missing from ${lang}`);
    }
});

test('the helper and platform answers say what to do instead of reconnecting', () => {
    const langs = dictionary();
    for (const [lang, texts] of Object.entries(langs)) {
        const helper = String(texts.ws_local_helper_missing || '');
        const platform = String(texts.ws_local_platform_unsupported || '');
        assert.match(helper, /fs-guard/, `${lang}: name the missing component`);
        assert.match(helper, /安装|建置|构建|install|rebuild/i, `${lang}: and say to install or rebuild`);
        assert.doesNotMatch(helper, /重新连接|重新連線|reconnect/i, `${lang}: reconnecting cannot fix it`);
        assert.ok(platform.length > 0, `${lang}: the platform answer must exist`);
    }
});
