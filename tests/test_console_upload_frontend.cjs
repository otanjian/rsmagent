// The console's fetch wrapper must attach the tenant selection header to every
// tenant-gated transport it issues, including the non-/api ones.
//
// Regression: `/upload` was omitted from the injection list, so pasting or
// picking a file in database mode sent a request the gate saw as "no tenant
// selected" (400 missing_tenant) and the console dropped the attachment
// silently. `/uploads/(.*)` is deliberately NOT in this list — it is read by
// the browser as an <img>/<audio> subresource, which cannot carry a header at
// all, and derives its tenant from the addressed Agent instead.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');

function section(start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from + start.length);
    assert.ok(from >= 0 && to > from, `Missing console section ${start}`);
    return source.slice(from, to);
}

// The wrapper itself, up to the fetch it delegates to.
const wrapperSource = section(
    'const _nativeFetch = window.fetch.bind(window);',
    'function generateSessionId()');

// Declarations the wrapper closes over, injected so the section can run alone.
const PREAMBLE = 'let activeAgentId = "chat-agent";\n';

function loadWrapper() {
    const calls = [];
    const sandbox = {
        window: {
            // The wrapper resolves every request against the page URL before it
            // decides same-origin and exempt paths, so the harness has to offer
            // a location (it was added to the wrapper after these tests were).
            location: { href: 'http://localhost/', origin: 'http://localhost' },
            fetch: (input, init) => {
                calls.push({ url: typeof input === 'string' ? input : input.url, init });
                return Promise.resolve({ ok: true });
            },
        },
        sessionStorage: { getItem: (key) => (key === 'cow_tenant_id' ? 'tnt_test' : null) },
        Headers,
        Request,
        URL,
        console,
    };
    sandbox.window.sessionStorage = sandbox.sessionStorage;
    vm.createContext(sandbox);
    vm.runInContext(PREAMBLE + wrapperSource, sandbox);
    return { fetch: sandbox.window.fetch, calls };
}

function headerOf(init, name) {
    const headers = init && init.headers;
    if (!headers) return null;
    if (headers instanceof Headers) return headers.get(name);
    if (Array.isArray(headers)) {
        const hit = headers.find(([k]) => k.toLowerCase() === name.toLowerCase());
        return hit ? hit[1] : null;
    }
    for (const key of Object.keys(headers)) {
        if (key.toLowerCase() === name.toLowerCase()) return headers[key];
    }
    return null;
}

test('upload carries the tenant selection header', async () => {
    const { fetch, calls } = loadWrapper();
    await fetch('/upload', { method: 'POST', body: new FormData() });
    assert.equal(calls.length, 1);
    assert.equal(headerOf(calls[0].init, 'X-Tenant-ID'), 'tnt_test');
});

test('chat transports keep the tenant selection header', async () => {
    for (const url of ['/message', '/stream', '/poll', '/cancel']) {
        const { fetch, calls } = loadWrapper();
        await fetch(url, { method: 'POST' });
        assert.equal(headerOf(calls[0].init, 'X-Tenant-ID'), 'tnt_test', url);
    }
});

test('api requests keep the tenant selection header', async () => {
    const { fetch, calls } = loadWrapper();
    await fetch('/api/sessions/abc/settings', { method: 'GET' });
    assert.equal(headerOf(calls[0].init, 'X-Tenant-ID'), 'tnt_test');
});

test('auth endpoints never receive the tenant selection header', async () => {
    const { fetch, calls } = loadWrapper();
    await fetch('/api/auth/login', { method: 'POST' });
    assert.equal(headerOf(calls[0].init, 'X-Tenant-ID'), null);
});

test('upload read-back is not given a header it cannot send', async () => {
    // Documented intent: /uploads is subresource-read, so the wrapper must not
    // be relied on there. Asserting it keeps the two paths from being conflated.
    const { fetch, calls } = loadWrapper();
    await fetch('/uploads/web_abc.png?agent_id=chat-agent', { method: 'GET' });
    assert.equal(headerOf(calls[0].init, 'X-Tenant-ID'), null);
});

test('the injected tenant header is not overwritten when already present', async () => {
    const { fetch, calls } = loadWrapper();
    await fetch('/upload', {
        method: 'POST',
        headers: { 'X-Tenant-ID': 'tnt_explicit' },
        body: new FormData(),
    });
    assert.equal(headerOf(calls[0].init, 'X-Tenant-ID'), 'tnt_explicit');
});
