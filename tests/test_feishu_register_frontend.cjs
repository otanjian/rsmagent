// Register-session handle plumbing (P0, task 2.8).
//
// The Feishu one-click register flow is bound to the caller's identity by an
// opaque handle the server issues on GET. Every poll must present that handle:
// the server has no "whoever asks first" fallback, so a poll without it reads as
// "expired" forever and the QR modal would spin until the user gives up. These
// assertions are source-level because the flow needs the Feishu SDK to run.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');

function functionBody(src, name) {
    const start = src.indexOf('function ' + name + '(');
    assert.ok(start >= 0, 'function ' + name + ' still exists');
    // Match braces from the first '{' after the parameter list.
    const open = src.indexOf('{', start);
    let depth = 0;
    let quote = null;
    for (let i = open; i < src.length; i++) {
        const c = src[i];
        if (quote) {
            if (c === '\\') { i++; continue; }
            if (c === quote) quote = null;
            continue;
        }
        if (c === '"' || c === "'" || c === '`') { quote = c; continue; }
        if (c === '/' && src[i + 1] === '/') {
            const nl = src.indexOf('\n', i);
            i = nl === -1 ? src.length : nl;
            continue;
        }
        if (c === '{') depth++;
        else if (c === '}') {
            depth--;
            if (depth === 0) return src.slice(open, i + 1);
        }
    }
    throw new Error('unbalanced braces in ' + name);
}

test('the start call stores the handle the server issued', () => {
    const body = functionBody(source, 'startFeishuRegister');
    assert.match(body, /_feishuRegisterHandle\s*=\s*data\.handle/,
        'GET response handle is not kept for polling');
    assert.match(body, /if\s*\(!_feishuRegisterHandle\)/,
        'a session without a handle would be polled anyway');
});

test('the poll presents the handle instead of an anonymous request', () => {
    const body = functionBody(source, 'pollFeishuRegisterStatus');
    assert.match(body, /action:\s*'poll',\s*handle:\s*handle/,
        'poll body does not carry the session handle');
    assert.match(body, /if\s*\(!_feishuRegisterHandle\)\s*\{[\s\S]*?return;/,
        'polling continues even though there is no session to address');
});

test('terminal states release the handle', () => {
    const body = functionBody(source, 'pollFeishuRegisterStatus');
    // done / expired / denied / error each end the session; a stale handle would
    // let a later poll keep asking the server about a session that is over.
    for (const status of ["'done'", "'expired'", "'denied'", "'error'"]) {
        const branch = body.indexOf('rs === ' + status);
        assert.ok(branch >= 0, 'missing branch for ' + status);
        // Up to the next branch: the reset belongs to this one.
        const next = body.indexOf('} else if (rs ===', branch + 1);
        const tail = body.slice(branch, next === -1 ? body.length : next);
        assert.match(tail, /_feishuRegisterHandle\s*=\s*''/,
            'handle not released on ' + status);
    }
});

test('leaving the scan tab drops the handle', () => {
    const body = functionBody(source, 'switchFeishuMode');
    assert.match(body, /_feishuRegisterHandle\s*=\s*''/,
        'switching away from the scan tab keeps a live handle');
});

// --- The scan start's target ------------------------------------------------
//
// Regression: the console's transport wrapper appends `?agent_id=<active>` to
// every same-origin /api call. The scan start reads agent_id as "the target
// this scan is for", so a shared scan arrived naming a personal target and the
// server refused it before any QR existed ("a shared scan must not name a
// personal target"). The target belongs to the form, never to the transport.
test('a shared scan names no target and the member scan names their own', () => {
    // `functionBody` yields the braces only; keep the parameter list so the
    // helper can be called the way the page calls it.
    const head = source.indexOf('function feishuScanStartUrl(');
    assert.ok(head >= 0, 'feishuScanStartUrl still exists');
    const body = functionBody(source, 'feishuScanStartUrl');
    const signature = source.slice(head, source.indexOf(body, head));
    const fn = new Function('encodeURIComponent',
        'return (' + signature + body + ')')(encodeURIComponent);
    assert.equal(fn('new', 'tenant', 'agent-1'),
        '/api/feishu/register?scope=tenant',
        'a shared scan must not carry the active/picked Agent');
    assert.equal(fn('', 'self', 'agent-1'),
        '/api/feishu/register?scope=tenant',
        'a scan that does not pre-fill a form stays shared');
    assert.equal(fn('new', 'self', 'agent 1'),
        '/api/feishu/register?scope=personal&agent_id=agent%201',
        'a member scan must name their own private Agent');
});

// The wrapper itself, up to the fetch it delegates to, with its closure
// declarations injected so the section can run alone.
function loadTransport(activeAgentId) {
    const from = source.indexOf('const _nativeFetch = window.fetch.bind(window);');
    const to = source.indexOf('function generateSessionId()', from);
    assert.ok(from >= 0 && to > from, 'the fetch wrapper still exists');
    const calls = [];
    const sandbox = {
        window: {
            location: { href: 'http://localhost/channels', origin: 'http://localhost' },
            fetch: (input, init) => {
                calls.push({ url: typeof input === 'string' ? input : input.url, init });
                return Promise.resolve({ ok: true });
            },
        },
        sessionStorage: { getItem: () => null },
        Headers,
        Request,
        URL,
        console,
    };
    sandbox.window.sessionStorage = sandbox.sessionStorage;
    vm.createContext(sandbox);
    vm.runInContext('let activeAgentId = ' + JSON.stringify(activeAgentId) + ';\n'
        + source.slice(from, to), sandbox);
    return { fetch: sandbox.window.fetch, calls };
}

test('the transport does not turn a scan start into a personal-target scan', async () => {
    const { fetch, calls } = loadTransport('chat-agent');
    await fetch('/api/feishu/register?scope=tenant', { method: 'GET' });
    assert.equal(calls[0].url, '/api/feishu/register?scope=tenant',
        'the active Agent was appended to the scan start');
});

test('the scan poll is not rewritten either', async () => {
    const { fetch, calls } = loadTransport('chat-agent');
    const body = JSON.stringify({ action: 'poll', handle: 'handle-1' });
    await fetch('/api/feishu/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
    });
    assert.equal(calls[0].url, '/api/feishu/register');
    assert.deepEqual(JSON.parse(calls[0].init.body),
        { action: 'poll', handle: 'handle-1' },
        'the poll body still carries the scan session only');
});

test('the exemption is the scan endpoints only', async () => {
    const { fetch, calls } = loadTransport('chat-agent');
    await fetch('/api/tenant/channels', { method: 'GET' });
    assert.match(calls[0].url, /[?&]agent_id=chat-agent/,
        'the channel write lost the active Agent');
});

test('a retry resumes the scan target the form started with', () => {
    const body = functionBody(source, 'renderFeishuRegisterError');
    assert.match(body, /startFeishuRegister\('\$\{statusId\}', _feishuScanTarget\)/,
        'retrying a form-started scan would drop back to a platform connect');
});
