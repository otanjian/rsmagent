// Account switching must confirm logout before returning to the same login URL.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/desktop-consent.js'), 'utf8');

function page(fetch) {
    const button = { disabled: false, addEventListener(event, fn) { this.click = fn; } };
    const error = { hidden: true };
    const decisions = [{ disabled: false }, { disabled: false }];
    const form = {
        querySelectorAll: () => decisions,
        setAttribute(name, value) { this[name] = value; },
        removeAttribute(name) { delete this[name]; },
    };
    let reloads = 0;
    vm.runInNewContext(source, {
        document: {
            getElementById: id => ({ 'desktop-switch-account': button, 'desktop-switch-error': error })[id] || null,
            querySelector: () => form,
        },
        fetch, location: { reload() { reloads += 1; } },
    });
    return { button, error, decisions, form, reloads: () => reloads };
}

test('switching is single-flight and blocks consent until logout confirms', async () => {
    const requests = [];
    let reply;
    const h = page((url, options) => {
        requests.push({ url, options });
        return new Promise(resolve => { reply = resolve; });
    });
    const pending = h.button.click();
    await h.button.click();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/auth/logout');
    assert.equal(requests[0].options.method, 'POST');
    assert.equal(requests[0].options.credentials, 'same-origin');
    assert.ok(h.decisions.every(button => button.disabled));
    assert.equal(h.reloads(), 0);
    reply({ ok: true, status: 200, json: async () => ({ status: 'success' }) });
    await pending;
    assert.equal(h.reloads(), 1);
});

test('unconfirmed logout keeps the page and offers a working retry', async () => {
    for (const fetch of [
        async () => { throw new Error('offline'); },
        async () => ({ ok: false, status: 503, json: async () => ({ status: 'error' }) }),
        async () => ({ ok: true, status: 200, json: async () => { throw new Error('not JSON'); } }),
        async () => ({ ok: true, status: 200, json: async () => ({ status: 'error' }) }),
    ]) {
        const h = page(fetch);
        await h.button.click();
        assert.equal(h.reloads(), 0);
        assert.equal(h.error.hidden, false);
        assert.equal(h.button.disabled, false);
        assert.ok(h.decisions.every(button => !button.disabled));
        assert.equal(h.form['aria-busy'], undefined);
    }
});

test('an expired session can return to login without a JSON error body', async () => {
    const h = page(async () => ({ ok: false, status: 401, json: async () => { throw new Error('expired'); } }));
    await h.button.click();
    assert.equal(h.reloads(), 1);
});

test('a notice page without the switch control needs no handlers', () => {
    vm.runInNewContext(source, { document: { getElementById: () => null } });
});
