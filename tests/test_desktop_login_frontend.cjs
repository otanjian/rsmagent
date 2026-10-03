// Exercise the browser login adapter without signing into a developer account.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/desktop-login.js'), 'utf8');

function page(fetch) {
    const nodes = new Map();
    function node(id) {
        if (!nodes.has(id)) {
            const classes = new Set(['hidden', 'fa-eye']);
            nodes.set(id, {
                value: '', textContent: '', type: 'password', disabled: false,
                classList: {
                    add: name => classes.add(name), remove: name => classes.delete(name),
                    contains: name => classes.has(name),
                    replace(from, to) { classes.delete(from); classes.add(to); },
                },
                setAttribute(name, value) { this[name] = value; },
                removeAttribute(name) { delete this[name]; },
                querySelector: () => node('password-icon'),
                focus() {},
            });
        }
        return nodes.get(id);
    }
    let reloads = 0;
    const window = {};
    vm.runInNewContext(source, {
        window, document: { getElementById: node }, fetch,
        location: { reload() { reloads += 1; } },
    });
    node('login-username').value = 'test-user';
    node('login-password').value = 'test-password';
    return { node, window, reloads: () => reloads,
        submit: () => node('login-form').onsubmit({ preventDefault() {} }) };
}

test('successful login submits once with same-origin cookies and reloads the authorization URL', async () => {
    const requests = [];
    let reply;
    const h = page((url, options) => {
        requests.push({ url, options });
        return new Promise(resolve => { reply = resolve; });
    });
    const pending = h.submit();
    await h.submit();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/auth/login');
    assert.equal(requests[0].options.credentials, 'same-origin');
    assert.deepEqual(JSON.parse(requests[0].options.body), { username: 'test-user', password: 'test-password' });
    reply({ ok: true, json: async () => ({ status: 'success', token: '' }) });
    await pending;
    assert.equal(h.reloads(), 1);
    assert.equal(h.node('login-password').value, '');
});

test('incorrect credentials stay on the shared form and permit another attempt', async () => {
    const h = page(async () => ({ ok: false, status: 401, json: async () => ({ status: 'error' }) }));
    await h.submit();
    assert.equal(h.reloads(), 0);
    assert.equal(h.node('login-error').classList.contains('hidden'), false);
    assert.equal(h.node('login-error').textContent, '登录信息有误，请重试');
    assert.equal(h.node('login-btn').disabled, false);
    assert.equal(h.node('login-password').value, '');
});

test('a network failure does not leave login disabled or navigate away', async () => {
    const h = page(async () => { throw new Error('offline'); });
    await h.submit();
    assert.equal(h.reloads(), 0);
    assert.equal(h.node('login-btn').disabled, false);
    assert.equal(h.node('login-form')['aria-busy'], undefined);
    assert.match(h.node('login-error').textContent, /网络/);
});

test('the shared password visibility control works without console globals', () => {
    const h = page(() => assert.fail('visibility must not make a request'));
    h.window.toggleLoginPassword();
    assert.equal(h.node('login-password').type, 'text');
    assert.equal(h.node('login-toggle-pwd')['aria-label'], '隐藏密码');
    h.window.toggleLoginPassword();
    assert.equal(h.node('login-password').type, 'password');
});
