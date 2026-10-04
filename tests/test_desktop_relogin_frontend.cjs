// The console's desktop re-login entry (change
// ``fix-desktop-relogin-session-sync``, tasks 2.3 / 2.4).
//
// The regression: inside a desktop container, signing out reloaded the page into
// the *ordinary* Web password form. That form signs in to the server only -- the
// native host stays signed out -- so the page looked signed in while the next
// local directory bind answered "not signed in", with no way out of the state.
//
// The shipped handlers are extracted from ``console.js`` and run for real, with
// one stand-in `window`, so these are the *actual* functions the container runs:
//
//   * a browser keeps the password form, unchanged;
//   * a container gets the re-login entry instead, and the password form is
//     refused rather than quietly creating the split session;
//   * the re-login ends the Web session and then the native one, in that order,
//     and never reloads;
//   * a host that cannot sign out says "update", and the page does not pretend
//     the workbench recovered.
//
// Run: node --test tests/test_desktop_relogin_frontend.cjs

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8',
);

/** One function's source, `async` included, by brace matching. */
function extract(name) {
    const asyncAt = source.indexOf(`async function ${name}(`);
    const from = asyncAt >= 0 ? asyncAt : source.indexOf(`function ${name}(`);
    assert.ok(from >= 0, `Missing ${name} in console.js`);
    let depth = 0;
    for (let i = source.indexOf('{', from); i < source.length; i++) {
        if (source[i] === '{') depth += 1;
        else if (source[i] === '}') {
            depth -= 1;
            if (depth === 0) return source.slice(from, i + 1);
        }
    }
    throw new Error(`Unbalanced ${name}`);
}

/** A minimal element with the class-list API the account view uses. */
function element(tag = 'div') {
    const classes = new Set(['hidden']);
    return {
        tagName: String(tag).toUpperCase(), id: '', value: '', textContent: '', dataset: {},
        disabled: false, children: [],
        classList: {
            add: (name) => classes.add(name),
            remove: (name) => classes.delete(name),
            contains: (name) => classes.has(name),
            toggle(name, force) {
                const add = force === undefined ? !classes.has(name) : force;
                if (add) classes.add(name); else classes.delete(name);
                return add;
            },
        },
        focus() {},
        appendChild(child) { this.children.push(child); return child; },
    };
}

/**
 * Run the shipped handlers against one stand-in environment.
 *
 * ``desktopAccount`` present means "this is a container": it is the fork module
 * the page asks, never a flag on the window, which is what makes the two paths
 * share one code path.
 */
function environment({ desktopAccount, host } = {}) {
    const nodes = new Map();
    const document = {
        activeElement: null,
        getElementById(id) {
            if (!nodes.has(id)) {
                const el = element(/(?:btn|retry|logout)$/.test(id) ? 'button' : 'div');
                el.id = id;
                nodes.set(id, el);
            }
            return nodes.get(id);
        },
        querySelector: () => null,
        addEventListener: () => undefined,
    };
    const reloads = [];
    const toasts = [];
    const fetches = [];
    const memoryResets = [];
    const win = {};
    if (host) win.CowDesktopHost = host;

    const ctx = {
        window: win,
        document,
        console,
        t: (key) => key,
        _authEpoch: 1,
        _accountWritePending: null,
        _pendingTenantPicker: false,
        _accountState: { phase: 'unauthenticated', mode: 'database', authRequired: false, authenticated: false, username: '' },
        _renderSidebarAccount: () => undefined,
        _invalidateAccountIdentity: () => undefined,
        _resetHistorySearch: () => undefined,
        _desktopContextClear: () => undefined,
        // Supplied by the memory page module in the real console.
        resetMemoryView: () => memoryResets.push('reset'),
        location: { reload: () => { reloads.push(1); } },
        fetch: async (url, options) => {
            fetches.push({ url, options });
            return { ok: true, status: 200, json: async () => ({ status: 'success' }) };
        },
        _wsToast: (message) => toasts.push(message),
    };
    ctx.window.setTimeout = (fn) => { void fn; return 0; };
    // The fork module is a *global* in the page, not a field of `window`, so it
    // is set the same way here.
    if (desktopAccount) {
        ctx.CowDesktopAccount = desktopAccount;
        win.CowDesktopAccount = desktopAccount;
    } else {
        ctx.CowDesktopAccount = undefined;
    }
    vm.createContext(ctx);
    vm.runInContext(
        "const _DESKTOP_SESSION_INVALID_CODES = ['auth_required', 'session_revoked', 'unauthorized', 'invalid_session'];",
        ctx,
    );
    vm.runInContext(
        ['_accountHidden', '_accountText', '_emptyAccount', 'showLoginScreen',
            '_desktopLoginRecovery', '_desktopLoginMode', '_desktopRequireSignIn',
            '_desktopAccountMessage', '_desktopSessionInvalid', '_submitAccountLogin',
            'desktopRelogin'].map(extract).join('\n\n'),
        ctx,
    );
    return { ctx, document, node: (id) => document.getElementById(id), reloads, toasts, fetches, memoryResets };
}

const visible = (h, id) => h.node(id).classList.contains('hidden') === false;

// ---------------------------------------------------------------------------
// A browser is untouched
// ---------------------------------------------------------------------------

test('a browser still gets the password form and no desktop entry', () => {
    const h = environment();
    h.ctx.showLoginScreen();
    assert.equal(visible(h, 'login-form'), true);
    assert.equal(visible(h, 'login-recovery'), false);
});

// ---------------------------------------------------------------------------
// A container gets the entry it can actually use
// ---------------------------------------------------------------------------

test('a container is shown the re-login entry instead of the password form', () => {
    const h = environment({ desktopAccount: { isDesktop: () => true, canSignOut: async () => true } });
    h.ctx.showLoginScreen();
    assert.equal(visible(h, 'login-form'), false, 'a Web-only login must not be offered');
    assert.equal(visible(h, 'login-recovery'), true);
});

test('a password submitted in a container is refused, not turned into a split session', async () => {
    const h = environment({ desktopAccount: { isDesktop: () => true, canSignOut: async () => true } });
    h.ctx.showLoginScreen();
    h.node('login-username').value = 'alice';
    h.node('login-password').value = 'secret';
    const handled = await h.ctx._submitAccountLogin({ preventDefault() {} });
    assert.equal(handled, false, 'the submission is refused');
    assert.equal(h.fetches.length, 0, 'no /auth/login is attempted: that is the split session');
    assert.equal(visible(h, 'login-recovery'), true);
    assert.equal(h.node('login-recovery-error').textContent, 'account_desktop_session_lost');
});

// ---------------------------------------------------------------------------
// The serial sign-out, driven through the real handler
// ---------------------------------------------------------------------------

test('the desktop re-login ends the Web session first, the host second, and never reloads', async () => {
    const order = [];
    const h = environment({
        desktopAccount: {
            isDesktop: () => true,
            canSignOut: async () => true,
            logout: async (options) => {
                order.push('logout');
                await options.webLogout();
                order.push('signOut');
                return { ok: true, signedOut: true, revoked: true };
            },
        },
    });
    h.node('login-recovery-btn').disabled = false;
    await h.ctx.desktopRelogin();
    assert.deepEqual(order, ['logout', 'signOut']);
    assert.deepEqual(h.memoryResets, ['reset'], 'account switching clears the previous memory view');
    assert.equal(h.fetches[0].url, '/auth/logout', 'the Web session is what is confirmed');
    assert.deepEqual(h.reloads, [], 'a reload would be the plain Web form this change removes');
    assert.equal(visible(h, 'login-recovery-error'), false, 'success says nothing');
});

test('a sign-out the host could not finish stays retryable and says why', async () => {
    const h = environment({
        desktopAccount: {
            isDesktop: () => true,
            canSignOut: async () => true,
            logout: async () => ({ ok: false, code: 'logout_incomplete', message: 'not confirmed' }),
        },
    });
    await h.ctx.desktopRelogin();
    assert.equal(visible(h, 'login-recovery-error'), true, 'the retry has to be visible');
    assert.equal(h.node('login-recovery-error').textContent, 'account_logout_unconfirmed');
    assert.equal(h.node('login-recovery-btn').disabled, false, 'the button is usable again');
    assert.deepEqual(h.reloads, []);
});

test('a desktop build without the action is told to update, not signed in anyway', async () => {
    const h = environment({
        desktopAccount: {
            isDesktop: () => true,
            canSignOut: async () => false,
            logout: async () => ({ ok: false, code: 'host_outdated' }),
        },
    });
    await h.ctx.desktopRelogin();
    assert.equal(h.node('login-recovery-error').textContent, 'account_desktop_upgrade_required');
    assert.deepEqual(h.reloads, []);
});

// ---------------------------------------------------------------------------
// Which codes mean "the login is gone"
// ---------------------------------------------------------------------------

test('only session codes ask for a re-login; permission and tenant codes do not', () => {
    const h = environment();
    for (const code of ['auth_required', 'session_revoked', 'unauthorized', 'invalid_session']) {
        assert.equal(h.ctx._desktopSessionInvalid(code), true, code);
    }
    for (const code of ['forbidden', 'invalid_tenant', 'device_offline', 'timeout',
        'feature_unavailable', 'logout_incomplete', '', undefined]) {
        assert.equal(h.ctx._desktopSessionInvalid(code), false, String(code));
    }
});

test('a lost login sends the page to the entry with the reason on it', () => {
    const h = environment({ desktopAccount: { isDesktop: () => true, canSignOut: async () => true } });
    h.ctx._desktopRequireSignIn();
    assert.equal(visible(h, 'login-overlay'), true);
    assert.equal(visible(h, 'login-recovery'), true);
    assert.equal(h.node('login-recovery-error').textContent, 'account_desktop_session_lost');
});
